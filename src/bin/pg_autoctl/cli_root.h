/*
 * src/bin/pg_autoctl/cli_root.h
 *     Implementation of a CLI which lets you run individual keeper routines
 *     directly
 *
 * Copyright (c) Microsoft Corporation. All rights reserved.
 * Licensed under the PostgreSQL License.
 *
 */

#ifndef CLI_ROOT_H
#define CLI_ROOT_H

#include "commandline.h"

extern char pg_autoctl_argv0[];

extern CommandLine help;
extern CommandLine version;

extern CommandLine create_commands;
extern CommandLine *create_subcommands[];

extern CommandLine show_commands;
extern CommandLine *show_subcommands[];

extern CommandLine drop_commands;
extern CommandLine *drop_subcommands[];

extern CommandLine root_with_debug;
extern CommandLine *root_subcommands_with_debug[];

extern CommandLine root;
extern CommandLine *root_subcommands[];

void keeper_cli_help(int argc, char **argv);
void keeper_cli_print_version(int argc, char **argv);
int root_options(int argc, char **argv);


#endif  /* CLI_ROOT_H */
