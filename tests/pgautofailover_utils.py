import os
import os.path
import signal
import shutil
import time
import network
import psycopg2
import subprocess
from enum import Enum

COMMAND_TIMEOUT = 60
STATE_CHANGE_TIMEOUT = 90

class Role(Enum):
    Monitor = 1
    Postgres = 2
    Coordinator = 3
    Worker = 4

    def command(self):
        return self.name.lower()

class Feature(Enum):
    Secondary = 1

    def command(self):
        return self.name.lower()

class Cluster:
    # Docker uses 172.17.0.0/16 by default, so we use 172.27.1.0/24 to not
    # conflict with that.
    def __init__(self, networkNamePrefix="pgauto", networkSubnet="172.27.1.0/24"):
        """
        Initializes the environment, virtual network, and other local state
        necessary for operation of the Cluster.
        """
        os.environ["PG_REGRESS_SOCK_DIR"] = ''
        os.environ["PG_AUTOCTL_DEBUG"] = ''
        os.environ["PGHOST"] = 'localhost'
        self.vlan = network.VirtualLAN(networkNamePrefix, networkSubnet)
        self.monitor = None
        self.datanodes = []

    def create_monitor(self, datadir, port=5432, nodename=None, authMethod=None):
        """
        Initializes the monitor and returns an instance of MonitorNode.
        """
        if self.monitor is not None:
            raise Exception("Monitor has already been created.")
        vnode = self.vlan.create_node()
        self.monitor = MonitorNode(datadir, vnode, port, nodename, authMethod)
        self.monitor.create()
        return self.monitor

    # TODO group should auto sense for normal operations and passed to the
    # create cli as an argument when explicitly set by the test
    def create_datanode(self, datadir, port=5432, group=0,
                        listen_flag=False, role=Role.Postgres, formation=None, authMethod=None):
        """
        Initializes a data node and returns an instance of DataNode. This will
        do the "keeper init" and "pg_autoctl run" commands.
        """
        vnode = self.vlan.create_node()
        nodeid = len(self.datanodes) + 1
        datanode = DataNode(datadir, vnode, port, os.getenv("USER"), authMethod, "postgres",
                            self.monitor, nodeid, group, listen_flag,
                            role, formation)
        self.datanodes.append(datanode)
        return datanode

    def destroy(self):
        """
        Cleanup whatever was created for this Cluster.
        """
        for datanode in self.datanodes:
            datanode.destroy()
        if self.monitor:
            self.monitor.destroy()
        self.vlan.destroy()

class PGNode:
    """
    Common stuff between MonitorNode and DataNode.
    """
    def __init__(self, datadir, vnode, port, username, authMethod, database, role):
        self.datadir = datadir
        self.vnode = vnode
        self.port = port
        self.username = username
        self.authMethod = authMethod
        self.database = database
        self.role = role
        self.pg_autoctl_run_proc = None
        self.authenticatedUsers = {}
            

    def connection_string(self):
        """
        Returns a connection string which can be used to connect to this postgres
        node.
        """
        if (self.authMethod and self.username in self.authenticatedUsers):
            return ("postgres://%s:%s@%s:%d/%s" % (self.username, self.authenticatedUsers[self.username], self.vnode.address,
                    self.port, self.database))
        
        return ("postgres://%s@%s:%d/%s" % (self.username, self.vnode.address,
                                           self.port, self.database))

    def run(self, env={}):
        """
        Runs "pg_autoctl run"
        """
        run_command = [shutil.which('pg_autoctl'), 'run',
                          '--pgdata', self.datadir]
        self.pg_autoctl_run_proc = self.vnode.run(run_command)

    def run_sql_query(self, query, *args):
        """
        Runs the given sql query with the given arguments in this postgres node
        and returns the results. Returns None if there are no results to fetch.
        """
        with psycopg2.connect(self.connection_string()) as conn:
            cur = conn.cursor()
            cur.execute(query, args)
            try:
                result = cur.fetchall()
                return result
            except psycopg2.ProgrammingError:
                return None

    def set_user_password(self, username, password):
        """
        Sets user passwords on the PGNode
        """
        alter_user_set_passwd_command =  "alter user %s with password \'%s\'" % (username, password)
        passwd_command = [shutil.which('psql'), '-d', self.database, '-c', alter_user_set_passwd_command]
        passwd_proc = self.vnode.run(passwd_command)
        wait_or_timeout_proc(passwd_proc,
                         name="user passwd",
                         timeout=COMMAND_TIMEOUT)
        self.authenticatedUsers[username] = password
    
    def stop_pg_autoctl(self):
        """
        Kills the keeper by sending a SIGTERM to keeper's process group.
        """
        if self.pg_autoctl_run_proc:
            os.killpg(os.getpgid(self.pg_autoctl_run_proc.pid), signal.SIGTERM)

    def stop_postgres(self):
        """
        Stops the postgres process by running:
          pg_ctl -D ${self.datadir} --wait --mode immediate stop
        """
        stop_command = [shutil.which('pg_ctl'), '-D', self.datadir,
                        '--wait', '--mode', 'immediate', 'stop']
        stop_proc = self.vnode.run(stop_command)
        out, err = stop_proc.communicate(timeout=COMMAND_TIMEOUT)
        if stop_proc.returncode > 0:
            print("stopping postgres for '%s' failed, out: %s\n, err: %s"
                  %(self.vnode.address, out, err))
            return False
        elif stop_proc.returncode is None:
            print("stopping postgres for '%s' timed out")
            return False
        return True

    def pg_is_running(self, timeout=COMMAND_TIMEOUT):
        """
        Returns true when Postgres is running. We use pg_ctl status.
        """
        status_command = [shutil.which('pg_ctl'), '-D', self.datadir, 'status']
        status_proc = self.vnode.run(status_command)
        out, err = status_proc.communicate(timeout=timeout)
        if status_proc.returncode == 0:
            # pg_ctl status is happy to report 0 (Postgres is running) even
            # when it's still "starting" and thus not ready for queries.
            #
            # because our tests need to be able to send queries to Postgres,
            # the "starting" status is not good enough for us, we're only
            # happy with "ready".
            pidfile = os.path.join(self.datadir, 'postmaster.pid')
            with open(pidfile, "r") as p:
                pg_status = p.readlines()[7]
            return pg_status.startswith("ready")
        elif status_proc.returncode > 0:
            # ignore `pg_ctl status` output, silently try again till timeout
            return False
        elif status_proc.returncode is None:
            print("pg_ctl status timed out after %ds" % timeout)
            return False

    def wait_until_pg_is_running(self, timeout=STATE_CHANGE_TIMEOUT):
        """
        Waits until the underlying Postgres process is running.
        """
        for i in range(timeout):
            time.sleep(1)

            if self.pg_is_running():
                return True

        else:
            print("Postgres is still not running in %s after %d attempts" %
                  (self.datadir, timeout))
            return False

    def fail(self):
        """
        Simulates a data node failure by terminating the keeper and stopping
        postgres.
        """
        self.stop_pg_autoctl()
        self.stop_postgres()

    def destroy(self):
        """
        Cleans up processes and files created for this data node.
        """
        self.stop_pg_autoctl()
        destroy_command = [shutil.which('pg_autoctl'), 'do', 'destroy',
                            '--pgdata', self.datadir]
        destroy_proc = self.vnode.run(destroy_command)
        try:
            wait_or_timeout_proc(destroy_proc,
                                 name="pg_autoctl do destroy",
                                 timeout=COMMAND_TIMEOUT)
        except Exception as e:
            print(str(e))
        try:
            os.remove(self.config_file_path())
        except FileNotFoundError:
            pass
        try:
            os.remove(self.state_file_path())
        except FileNotFoundError:
            pass


    def config_file_path(self):
        """
        Returns the path of the config file for this data node.
        """
        # Config file is located at:
        # ~/.config/pg_autoctl/${PGDATA}/pg_autoctl.cfg
        home = os.getenv("HOME")
        pgdata = os.path.abspath(self.datadir)[1:] # Remove the starting '/'
        return os.path.join(home,
                            ".config/pg_autoctl",
                            pgdata,
                            "pg_autoctl.cfg")

    def state_file_path(self):
        """
        Returns the path of the state file for this data node.
        """
        # State file is located at:
        # ~/.local/share/pg_autoctl/${PGDATA}/pg_autoctl.state
        home = os.getenv("HOME")
        pgdata = os.path.abspath(self.datadir)[1:] # Remove the starting '/'
        return os.path.join(home,
                            ".local/share/pg_autoctl",
                            pgdata,
                            "pg_autoctl.state")

class DataNode(PGNode):
    def __init__(self, datadir, vnode, port, username, authMethod, database, monitor,
                 nodeid, group, listen_flag, role, formation):
        super().__init__(datadir, vnode, port, username, authMethod, database, role)
        self.monitor = monitor
        self.nodeid = nodeid
        self.group = group
        self.listen_flag = listen_flag
        self.formation = formation

    def create(self):
        """
        Runs "pg_autoctl create"
        """
        pghost = 'localhost'

        if self.listen_flag:
            pghost = str(self.vnode.address)

        # don't pass --nodename to Postgres nodes in order to exercise the
        # automatic detection of the nodename.
        create_command = [shutil.which('pg_autoctl'), '-vvv', 'create',
                          self.role.command(),
                        '--pgdata', self.datadir,
                        '--pghost', pghost,
                        '--pgport', str(self.port),
                        '--pgctl', shutil.which('pg_ctl'),
                        '--monitor', self.monitor.connection_string()]

        if self.listen_flag:
            create_command += ['--listen', str(self.vnode.address)]

        if self.formation:
            create_command += ['--formation', self.formation]

        init_proc = self.vnode.run(create_command)
        wait_or_timeout_proc(init_proc,
                             name="keeper init",
                             timeout=COMMAND_TIMEOUT)


    def wait_until_state(self, target_state, timeout=STATE_CHANGE_TIMEOUT):
        """
        Waits until this data node reaches the target state, and then returns
        True. If this doesn't happen until "timeout" seconds, returns False.
        """
        prev_state = None
        for i in range(timeout):
            time.sleep(1)
            current_state = self.get_state()

            # only log the state if it has changed
            if current_state != prev_state:
                print("state of %s is '%s', waiting for '%s' ..." %
                    (self.datadir, current_state, target_state))

            if current_state == target_state:
                return True
            prev_state = current_state
        else:
            print("%s didn't reach %s after %d attempts" %
                (self.datadir, target_state, timeout))
            return False

    def get_state(self):
        """
        Returns the current state of the data node. This is done by querying the
        monitor node.
        """
        results = self.monitor.run_sql_query(
            """
SELECT reportedstate
  FROM pgautofailover.node
 WHERE nodeid=%s and groupid=%s
""",
            self.nodeid, self.group)
        if len(results) == 0:
            raise Exception("datanode not found at coordinator")
        else:
            return results[0][0]
        return results

    def enable_maintenance(self):
        """
        Enables maintenance on a pg_autoctl standby node

        :return:
        """
        command = [shutil.which('pg_autoctl'), 'enable', 'maintenance',
                   '--pgdata', self.datadir]
        proc = self.vnode.run(command)
        wait_or_timeout_proc(proc,
                             name="enable maintenance",
                             timeout=COMMAND_TIMEOUT)

    def disable_maintenance(self):
        """
        Disables maintenance on a pg_autoctl standby node

        :return:
        """
        command = [shutil.which('pg_autoctl'), 'disable', 'maintenance',
                   '--pgdata', self.datadir]
        proc = self.vnode.run(command)
        wait_or_timeout_proc(proc,
                             name="disable maintenance",
                             timeout=COMMAND_TIMEOUT)

    def drop(self):
        """
        Drops a pg_autoctl node from its formation

        :return:
        """
        drop_command = [shutil.which('pg_autoctl'), 'drop', 'node',
                       '--pgdata', self.datadir]
        drop_proc = self.vnode.run(drop_command)
        wait_or_timeout_proc(drop_proc, name="drop node", timeout=COMMAND_TIMEOUT)


class MonitorNode(PGNode):
    def __init__(self, datadir, vnode, port, nodename, authMethod):
           
        super().__init__(datadir, vnode, port,
                         "autoctl_node", authMethod, "pg_auto_failover", Role.Monitor)

        # set the nodename, default to the ip address of the node
        if nodename:
            self.nodename = nodename
        else:
            self.nodename = str(self.vnode.address)


    def create(self):
        """
        Initializes and runs the monitor process.
        """
        init_command = [shutil.which('pg_autoctl'), '-vvv', 'create',
                        self.role.command(),
                        '--pgdata', self.datadir,
                        '--pgport', str(self.port),
                        '--nodename', self.nodename]
        
        if self.authMethod:
            init_command.extend(['--auth', self.authMethod])    
        
        init_proc = self.vnode.run(init_command)
        wait_or_timeout_proc(init_proc,
                             name="create monitor",
                             timeout=COMMAND_TIMEOUT)
      

    def create_formation(self, formation_name,
                         kind="pgsql", secondary=None, dbname=None):
        """
        Create a formation that the monitor controls

        :param formation_name: identifier used to address the formation
        :param ha: boolean whether or not to run the formation with high availability
        :param kind: identifier to signal what kind of formation to run
        :param dbname: name of the database to use in the formation
        :return: None
        """
        formation_command = [shutil.which('pg_autoctl'), 'create', 'formation',
                             '--pgdata', self.datadir,
                             '--formation', formation_name,
                             '--kind', kind]

        if dbname is not None:
            formation_command += ['--dbname', dbname]

        # pass true or false to --enable-secondary or --disable-secondary, only when ha is
        # actually set by the user
        if secondary is not None:
            if secondary:
                formation_command += ['--enable-secondary']
            else:
                formation_command += ['--disable-secondary']

        formation_proc = self.vnode.run(formation_command)
        wait_or_timeout_proc(formation_proc,
                             name="create formation",
                             timeout=COMMAND_TIMEOUT)

    def enable(self, feature, formation='default'):
        """
        Enable a feature on a formation

        :param feature: instance of Feature enum indicating which feature to enable
        :param formation: name of the formation to enable the feature on
        :return: None
        """
        enable_command = [shutil.which('pg_autoctl'), 'enable', feature.command(),
                          '--pgdata', self.datadir,
                          '--formation', formation]

        print("enable_command:", enable_command)

        enable_proc = self.vnode.run(enable_command)
        wait_or_timeout_proc(enable_proc,
                             name="enable feature",
                             timeout=COMMAND_TIMEOUT)

    def disable(self, feature, formation='default'):
        """
        Disable a feature on a formation

        :param feature: instance of Feature enum indicating which feature to disable
        :param formation: name of the formation to disable the feature on
        :return: None
        """
        disable_command = [shutil.which('pg_autoctl'), 'disable', feature.command(),
                          '--pgdata', self.datadir,
                          '--formation', formation]

        disable_proc = self.vnode.run(disable_command)
        wait_or_timeout_proc(disable_proc,
                             name="disable feature",
                             timeout=COMMAND_TIMEOUT)

    def failover(self, formation='default', group=0):
        """
        performs manual failover for given formation and group id
        """
        failover_commmand_text = "select * from pgautofailover.perform_failover('%s', %s)" %(formation, group)
        failover_command = [shutil.which('psql'), '-d', self.database, '-c', failover_commmand_text]
        failover_proc = self.vnode.run(failover_command)
        wait_or_timeout_proc(failover_proc,
                         name="manual failover",
                         timeout=COMMAND_TIMEOUT)



def wait_or_timeout_proc(proc, name, timeout):
    """
    Waits for command to exit successfully. If it exits with error or it timeouts,
    raises an execption with stdout and stderr streams of the process.
    """
    try:
        out, err = proc.communicate(timeout=COMMAND_TIMEOUT)
        if proc.returncode > 0:
            raise Exception("%s failed, out: %s\n, err: %s" % (name, out, err))
        return out, err
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise Exception("%s timed out after %d seconds. out: %s\n, err: %s" \
                        % (name, timeout, out, err))
