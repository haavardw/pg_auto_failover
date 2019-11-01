/*-------------------------------------------------------------------------
 *
 * src/monitor/node_metadata.h
 *
 * Declarations for public functions and types related to node metadata.
 *
 * Copyright (c) Microsoft Corporation. All rights reserved.
 * Licensed under the PostgreSQL License.
 *
 *-------------------------------------------------------------------------
 */

#pragma once

#include "access/xlogdefs.h"
#include "datatype/timestamp.h"

#include "health_check.h"
#include "replication_state.h"

#define AUTO_FAILOVER_NODE_TABLE_NAME "node"

/* column indexes for pgautofailover.node */
#define Natts_pgautofailover_node 15
#define Anum_pgautofailover_node_formationid 1
#define Anum_pgautofailover_node_nodeid 2
#define Anum_pgautofailover_node_groupid 3
#define Anum_pgautofailover_node_nodename 4
#define Anum_pgautofailover_node_nodeport 5
#define Anum_pgautofailover_node_goalstate 6
#define Anum_pgautofailover_node_reportedstate 7
#define Anum_pgautofailover_node_reportedpgisrunning 8
#define Anum_pgautofailover_node_reportedrepstate 9
#define Anum_pgautofailover_node_reporttime 10
#define Anum_pgautofailover_node_reportedLSN 11
#define Anum_pgautofailover_node_walreporttime 12
#define Anum_pgautofailover_node_health 13
#define Anum_pgautofailover_node_healthchecktime 14
#define Anum_pgautofailover_node_statechangetime 15
#define Anum_pgautofailover_node_candidate_priority 16
#define Anum_pgautofailover_node_replication_quorum 17


/* pg_stat_replication.sync_state: "sync", "async", "quorum", "potential" */
typedef enum SyncState
{
	SYNC_STATE_UNKNOWN = 0,
	SYNC_STATE_SYNC,
	SYNC_STATE_ASYNC,
	SYNC_STATE_QUORUM,
	SYNC_STATE_POTENTIAL
} SyncState;


/*
 * AutoFailoverNode represents a Postgres node that is being tracked by the
 * pg_auto_failover monitor.
 */
typedef struct AutoFailoverNode
{
	char *formationId;
	int nodeId;
	int groupId;
	char *nodeName;
	int nodePort;
	ReplicationState goalState;
	ReplicationState reportedState;
	TimestampTz reportTime;
	bool pgIsRunning;
	SyncState pgsrSyncState;
	XLogRecPtr reportedLSN;
	TimestampTz walReportTime;
	NodeHealthState health;
	TimestampTz healthCheckTime;
	TimestampTz stateChangeTime;
	int candidatePriority;
	bool replicationQuorum;
} AutoFailoverNode;


/* public function declarations */
extern List * AllAutoFailoverNodes(char *formationId);
extern List * AutoFailoverNodeGroup(char *formationId, int groupId);
extern AutoFailoverNode * GetAutoFailoverNode(char *nodeName, int nodePort);
extern AutoFailoverNode * OtherNodeInGroup(AutoFailoverNode *pgAutoFailoverNode);
extern AutoFailoverNode * TupleToAutoFailoverNode(TupleDesc tupleDescriptor,
												  HeapTuple heapTuple);
extern int AddAutoFailoverNode(char *formationId, int groupId,
							   char *nodeName, int nodePort,
							   ReplicationState goalState,
							   ReplicationState reportedState,
							   int candidatePriority,
							   bool replicationQuorum);
extern void SetNodeGoalState(char *nodeName, int nodePort,
							 ReplicationState goalState);
extern void ReportAutoFailoverNodeState(char *nodeName, int nodePort,
										ReplicationState reportedState,
										bool pgIsRunning,
										SyncState pgSyncState,
										XLogRecPtr reportedLSN);
extern void ReportAutoFailoverNodeHealth(char *nodeName, int nodePort,
										 ReplicationState goalState,
										 NodeHealthState health);
extern void ReportAutoFailoverNodeReplicationState(char *nodeName, int nodePort,
												   int candidatePriority,
												   bool replicationQuorum);
extern void RemoveAutoFailoverNode(char *nodeName, int nodePort);

extern SyncState SyncStateFromString(const char *pgsrSyncState);
extern char *SyncStateToString(SyncState pgsrSyncState);
extern bool IsCurrentState(AutoFailoverNode *pgAutoFailoverNode,
						   ReplicationState state);
