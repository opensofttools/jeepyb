#! /usr/bin/env python
# Copyright (C) 2011 OpenStack, LLC.
# Copyright (c) 2012 Hewlett-Packard Development Company, L.P.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# manage_projects.py reads a project config file called projects.yaml
# It should look like:

# - homepage: http://openstack.org
#   gerrit-host: review.openstack.org
#   local-git-dir: /var/lib/git
#   gerrit-key: /home/gerrit2/review_site/etc/ssh_host_rsa_key
#   gerrit-committer: Project Creator <openstack-infra@lists.openstack.org>
#   has-github: True
#   has-wiki: False
#   has-issues: False
#   has-downloads: False
#   acl-dir: /home/gerrit2/acls
#   acl-base: /home/gerrit2/acls/project.config
# ---
# - project: PROJECT_NAME
#   options:
#    - has-wiki
#    - has-issues
#    - has-downloads
#    - has-pull-requests
#    - track-upstream
#   homepage: Some homepage that isn't http://openstack.org
#   description: This is a great project
#   upstream: https://gerrit.googlesource.com/gerrit
#   upstream-prefix: upstream
#   acl-config: /path/to/gerrit/project.config
#   acl-append:
#     - /path/to/gerrit/project.config
#   acl-parameters:
#     project: OTHER_PROJECT_NAME

import argparse
import ConfigParser
import logging
import os
import re
import shlex
import subprocess
import tempfile
import time
import yaml

import gerritlib.gerrit
import github

import jeepyb.gerritdb

log = logging.getLogger("manage_projects")


def run_command(cmd, status=False, env={}):
    cmd_list = shlex.split(str(cmd))
    newenv = os.environ
    newenv.update(env)
    log.debug("Executing command: %s" % " ".join(cmd_list))
    p = subprocess.Popen(cmd_list, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=newenv)
    (out, nothing) = p.communicate()
    log.debug("Return code: %s" % p.returncode)
    log.debug("Command said: %s" % out.strip())
    if status:
        return (p.returncode, out.strip())
    return out.strip()


def run_command_status(cmd, env={}):
    return run_command(cmd, True, env)


def git_command(repo_dir, sub_cmd, env={}):
    git_dir = os.path.join(repo_dir, '.git')
    cmd = "git --git-dir=%s --work-tree=%s %s" % (git_dir, repo_dir, sub_cmd)
    status, _ = run_command(cmd, True, env)
    return status


def git_command_output(repo_dir, sub_cmd, env={}):
    git_dir = os.path.join(repo_dir, '.git')
    cmd = "git --git-dir=%s --work-tree=%s %s" % (git_dir, repo_dir, sub_cmd)
    status, out = run_command(cmd, True, env)
    return (status, out)


def write_acl_config(project, acl_dir, acl_base, acl_append, parameters):
    project_parts = os.path.split(project)
    if len(project_parts) > 1:
        repo_base = os.path.join(acl_dir, *project_parts[:-1])
        if not os.path.exists(repo_base):
            os.makedirs(repo_base)
        if not os.path.isdir(repo_base):
            return 1
        project = project_parts[-1]
        config_file = os.path.join(repo_base, "%s.config" % project)
    else:
        config_file = os.path.join(acl_dir, "%s.config" % project)
    if 'project' not in parameters:
        parameters['project'] = project
    with open(config_file, 'w') as config:
        if acl_base and os.path.exists(acl_base):
            config.write(open(acl_base, 'r').read())
        for acl_snippet in acl_append:
            if not os.path.exists(acl_snippet):
                acl_snippet = os.path.join(acl_dir, acl_snippet)
            if not os.path.exists(acl_snippet):
                continue
            with open(acl_snippet, 'r') as append_content:
                config.write(append_content.read() % parameters)


def fetch_config(project, remote_url, repo_path, env={}):
    status = git_command(repo_path, "fetch %s +refs/meta/config:"
                         "refs/remotes/gerrit-meta/config" % remote_url, env)
    if status != 0:
        log.error("Failed to fetch refs/meta/config for project: %s" % project)
        return False
    # Because the following fails if executed more than once you should only
    # run fetch_config once in each repo.
    status = git_command(repo_path, "checkout -B config "
                         "remotes/gerrit-meta/config")
    if status != 0:
        log.error("Failed to checkout config for project: %s" % project)
        return False

    return True


def copy_acl_config(project, repo_path, acl_config):
    if not os.path.exists(acl_config):
        return False

    acl_dest = os.path.join(repo_path, "project.config")
    status, _ = run_command("cp %s %s" %
                            (acl_config, acl_dest), status=True)
    if status == 0:
        status = git_command(repo_path, "diff --quiet")
        if status != 0:
            return True
    return False


def push_acl_config(project, remote_url, repo_path, gitid, env={}):
    cmd = "commit -a -m'Update project config.' --author='%s'" % gitid
    status = git_command(repo_path, cmd)
    if status != 0:
        log.error("Failed to commit config for project: %s" % project)
        return False
    status, out = git_command_output(repo_path,
                                     "push %s HEAD:refs/meta/config" %
                                     remote_url, env)
    if status != 0:
        log.error("Failed to push config for project: %s" % project)
        return False
    return True


def _get_group_uuid(group):
    cursor = jeepyb.gerritdb.connect().cursor()
    query = "SELECT group_uuid FROM account_groups WHERE name = %s"
    cursor.execute(query, group)
    data = cursor.fetchone()
    if data:
        return data[0]
    return None


def _wait_for_group(gerrit, group):
    """Wait for up to 10 seconds for the group to be created."""
    for x in range(10):
        groups = gerrit.listGroups()
        if group in groups:
            break
        time.sleep(1)


def get_group_uuid(gerrit, group):
    uuid = _get_group_uuid(group)
    if uuid:
        return uuid
    gerrit.createGroup(group)
    _wait_for_group(gerrit, group)
    uuid = _get_group_uuid(group)
    if uuid:
        return uuid
    return None


def create_groups_file(project, gerrit, repo_path):
    acl_config = os.path.join(repo_path, "project.config")
    group_file = os.path.join(repo_path, "groups")
    uuids = {}
    for line in open(acl_config, 'r'):
        r = re.match(r'^\s+.*group\s+(.*)$', line)
        if r:
            group = r.group(1)
            if group in uuids.keys():
                continue
            uuid = get_group_uuid(gerrit, group)
            if uuid:
                uuids[group] = uuid
            else:
                log.error("Unable to get UUID for group %s." % group)
                return False
    if uuids:
        with open(group_file, 'w') as fp:
            for group, uuid in uuids.items():
                fp.write("%s\t%s\n" % (uuid, group))
    status = git_command(repo_path, "add groups")
    if status != 0:
        log.error("Failed to add groups file for project: %s" % project)
        return False
    return True


def make_ssh_wrapper(gerrit_user, gerrit_key):
    (fd, name) = tempfile.mkstemp(text=True)
    os.write(fd, '#!/bin/bash\n')
    os.write(fd,
             'ssh -i %s -l %s -o "StrictHostKeyChecking no" $@\n' %
             (gerrit_key, gerrit_user))
    os.close(fd)
    os.chmod(name, 0o755)
    return dict(GIT_SSH=name)


def create_github_project(defaults, options, project, description, homepage):
    default_has_issues = defaults.get('has-issues', False)
    default_has_downloads = defaults.get('has-downloads', False)
    default_has_wiki = defaults.get('has-wiki', False)
    has_issues = 'has-issues' in options or default_has_issues
    has_downloads = 'has-downloads' in options or default_has_downloads
    has_wiki = 'has-wiki' in options or default_has_wiki

    GITHUB_SECURE_CONFIG = defaults.get(
        'github-config',
        '/etc/github/github-projects.secure.config')

    secure_config = ConfigParser.ConfigParser()
    secure_config.read(GITHUB_SECURE_CONFIG)

    # Project creation doesn't work via oauth
    ghub = github.Github(secure_config.get("github", "username"),
                         secure_config.get("github", "password"))
    orgs = ghub.get_user().get_orgs()
    orgs_dict = dict(zip([o.login.lower() for o in orgs], orgs))

    # Find the project's repo
    project_split = project.split('/', 1)
    org_name = project_split[0]
    if len(project_split) > 1:
        repo_name = project_split[1]
    else:
        repo_name = project

    try:
        org = orgs_dict[org_name.lower()]
    except KeyError:
        # We do not have control of this github org ignore the project.
        return
    try:
        repo = org.get_repo(repo_name)
    except github.GithubException:
        repo = org.create_repo(repo_name,
                               homepage=homepage,
                               has_issues=has_issues,
                               has_downloads=has_downloads,
                               has_wiki=has_wiki)
    if description:
        repo.edit(repo_name, description=description)
    if homepage:
        repo.edit(repo_name, homepage=homepage)

    repo.edit(repo_name, has_issues=has_issues,
              has_downloads=has_downloads,
              has_wiki=has_wiki)

    if 'gerrit' not in [team.name for team in repo.get_teams()]:
        teams = org.get_teams()
        teams_dict = dict(zip([t.name.lower() for t in teams], teams))
        teams_dict['gerrit'].add_to_repos(repo)


# TODO(mordred): Inspect repo_dir:master for a description
#                override
def find_description_override(repo_path):
    return None


def main():
    parser = argparse.ArgumentParser(description='Manage projects')
    parser.add_argument('-v', dest='verbose', action='store_true',
                        help='verbose output')
    parser.add_argument('--nocleanup', action='store_true',
                        help='do not remove temp directories')
    parser.add_argument('projects', metavar='project', nargs='*',
                        help='name of project(s) to process')
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.ERROR)

    PROJECTS_YAML = os.environ.get('PROJECTS_YAML',
                                   '/home/gerrit2/projects.yaml')
    configs = [config for config in yaml.load_all(open(PROJECTS_YAML))]
    defaults = configs[0][0]
    default_has_github = defaults.get('has-github', True)

    LOCAL_GIT_DIR = defaults.get('local-git-dir', '/var/lib/git')
    JEEPYB_CACHE_DIR = defaults.get('jeepyb-cache-dir', '/var/lib/jeepyb')
    ACL_DIR = defaults.get('acl-dir')
    GERRIT_HOST = defaults.get('gerrit-host')
    GERRIT_PORT = int(defaults.get('gerrit-port', '29418'))
    GERRIT_USER = defaults.get('gerrit-user')
    GERRIT_KEY = defaults.get('gerrit-key')
    GERRIT_GITID = defaults.get('gerrit-committer')
    GERRIT_SYSTEM_USER = defaults.get('gerrit-system-user', 'gerrit2')
    GERRIT_SYSTEM_GROUP = defaults.get('gerrit-system-group', 'gerrit2')

    gerrit = gerritlib.gerrit.Gerrit('localhost',
                                     GERRIT_USER,
                                     GERRIT_PORT,
                                     GERRIT_KEY)
    project_list = gerrit.listProjects()
    ssh_env = make_ssh_wrapper(GERRIT_USER, GERRIT_KEY)
    try:

        for section in configs[1]:
            project = section['project']
            if args.projects and project not in args.projects:
                continue
            options = section.get('options', dict())
            description = section.get('description', None)
            homepage = section.get('homepage', defaults.get('homepage', None))
            upstream = section.get('upstream', None)
            upstream_prefix = section.get('upstream-prefix', None)
            track_upstream = 'track-upstream' in options

            project_git = "%s.git" % project
            remote_url = "ssh://localhost:%s/%s" % (GERRIT_PORT, project)

            # Create the project in Gerrit first, since it will fail
            # spectacularly if its project directory or local replica
            # already exist on disk
            project_created = False
            if project not in project_list:
                try:
                    gerrit.createProject(project)
                    project_created = True
                except Exception:
                    log.exception(
                        "Exception creating %s in Gerrit." % project)

            # Create the repo for the local git mirror
            git_mirror_path = os.path.join(LOCAL_GIT_DIR, project_git)
            if not os.path.exists(git_mirror_path):
                run_command("git --bare init %s" % git_mirror_path)
                run_command("chown -R %s:%s %s"
                            % (GERRIT_SYSTEM_USER, GERRIT_SYSTEM_GROUP,
                               git_mirror_path))

            # Start processing the local JEEPYB cache
            repo_path = os.path.join(JEEPYB_CACHE_DIR, project)
            git_opts = dict(upstream=upstream,
                            repo_path=repo_path,
                            remote_url=remote_url)

            # If we don't have a local copy already, get one
            if not os.path.exists(repo_path) or project_created:
                if not os.path.exists(os.path.dirname(repo_path)):
                    os.makedirs(os.path.dirname(repo_path))
                # Three choices
                #  - If gerrit has it, get from gerrit
                #  - If gerrit doesn't have it:
                #    - If it has an upstream, clone that
                #    - If it doesn't, create it

                # Gerrit knows about the project, clone it
                if project in project_list:
                    run_command(
                        "git clone %(remote_url)s %(repo_path)s" % git_opts,
                        env=ssh_env)
                    if upstream:
                        git_command(
                            repo_path,
                            "remote add -f upstream %(upstream)s" % git_opts)

                # Gerrit doesn't have it, but it has an upstream configured
                # We're probably importing it for the first time, clone
                # upstream, but then ongoing we want gerrit to ge origin
                # and upstream to be only there for ongoing tracking
                # purposes, so rename origin to upstream and add a new
                # origin remote that points at gerrit
                elif upstream:
                    run_command(
                        "git clone %(upstream)s %(repo_path)s" % git_opts,
                        env=ssh_env)
                    git_command(
                        repo_path,
                        "fetch origin +refs/heads/*:refs/copy/heads/*",
                        env=ssh_env)
                    git_command(repo_path, "remote rename origin upstream")
                    git_command(
                        repo_path,
                        "remote add origin %(remote_url)s" % git_opts)
                    push_string = "push %s +refs/copy/heads/*:refs/heads/*"

                # Neither gerrit has it, nor does it have an upstream,
                # just create a whole new one
                else:
                    run_command("git init %s" % repo_path)
                    git_command(
                        repo_path,
                        "remote add origin %(remote_url)s" % git_opts)
                    with open(os.path.join(repo_path,
                                           ".gitreview"),
                              'w') as gitreview:
                        gitreview.write("""[gerrit]
host=%s
port=%s
project=%s
""" % (GERRIT_HOST, GERRIT_PORT, project_git))
                    git_command(repo_path, "add .gitreview")
                    cmd = ("commit -a -m'Added .gitreview' --author='%s'"
                           % GERRIT_GITID)
                    git_command(repo_path, cmd)
                    push_string = "push %s HEAD:refs/heads/master"

            # We do have a local copy of it already, make sure it's
            # in shape to have work done.
            else:
                if project in project_list:

                    # If we're configured to track upstream but the repo
                    # does not have an upstream remote, add one
                    if (track_upstream and
                        'upstream' not in git_command_output(
                            repo_path, 'remote')[1]):
                        git_command(
                            repo_path,
                            "remote add upstream %(upstream)s" % git_opts)

                    # If we're configured to track upstream, make sure that
                    # the upstream URL matches the config
                    else:
                        git_command(
                            repo_path,
                            "remote set-url upstream %(upstream)s" % git_opts)

                    # Now that we have any upstreams configured, fetch all
                    # of the refs we might need, pruning remote branches
                    # that no longer exist
                    git_command(
                        repo_path, "remote update --prune", env=ssh_env)

                    # TODO(mordred): This is here so that later we can
                    # inspect the master branch for meta-info
                    # Checkout master and reset to the state of origin/master
                    git_command(repo_path, "checkout -B master origin/master")

            description = find_description_override(repo_path) or description

            if 'has-github' in options or default_has_github:
                create_github_project(defaults, options, project,
                                      description, homepage)

            if project_created:
                try:
                    git_command(repo_path,
                                push_string % remote_url,
                                env=ssh_env)
                    git_command(repo_path,
                                "push --tags %s" % remote_url, env=ssh_env)
                except Exception:
                    log.exception(
                        "Error pushing %s to Gerrit." % project)

            # If we're configured to track upstream, make sure we have
            # upstream's refs, and then push them to the appropriate
            # branches in gerrit
            if track_upstream:
                git_command(
                    repo_path,
                    "remote update upstream --prune", env=ssh_env)
                # Any branch that exists in the upstream remote, we want
                # a local branch of, optionally prefixed with the
                # upstream prefix value
                for branch in git_command_output(
                        repo_path, "branch -a")[1].split():
                    if not branch.strip().startswith("remotes/upstream"):
                        continue
                    if "->" in branch:
                        continue
                    local_branch = branch[len('remotes/upstream/'):]
                    if upstream_prefix:
                        local_branch = "%s/%s" % (
                            upstream_prefix, local_branch)

                    # Check out an up to date copy of the branch, so that
                    # we can push it and it will get picked up below
                    git_command(repo_path, "checkout -B %s %s" % (
                        local_branch, branch))

                try:
                    # Push all of the local branches to similarly named
                    # Branches on gerrit. Also, push all of the tags
                    git_command(
                        repo_path,
                        "push origin refs/heads/*:refs/heads/*",
                        env=ssh_env)
                    git_command(repo_path, 'push origin --tags', env=ssh_env)
                except Exception:
                    log.exception(
                        "Error pushing %s to Gerrit." % project)

            try:
                acl_config = section.get('acl-config',
                                         '%s.config' % os.path.join(ACL_DIR,
                                                                    project))
            except AttributeError:
                acl_config = None

            if acl_config:
                if not os.path.isfile(acl_config):
                    write_acl_config(project,
                                     ACL_DIR,
                                     section.get('acl-base', None),
                                     section.get('acl-append', []),
                                     section.get('acl-parameters', {}))
                try:
                    if (fetch_config(project,
                                     remote_url,
                                     repo_path,
                                     ssh_env) and
                        copy_acl_config(project, repo_path,
                                        acl_config) and
                            create_groups_file(project, gerrit, repo_path)):
                        push_acl_config(project,
                                        remote_url,
                                        repo_path,
                                        GERRIT_GITID,
                                        ssh_env)
                except Exception:
                    log.exception(
                        "Exception processing ACLS for %s." % project)
                finally:
                    git_command(repo_path, 'reset --hard')
                    git_command(repo_path, 'checkout master')
                    git_command(repo_path, 'branch -D config')

    finally:
        os.unlink(ssh_env['GIT_SSH'])

if __name__ == "__main__":
    main()
