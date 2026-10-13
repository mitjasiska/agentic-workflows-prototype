"""Keep controller GitHub API tokens out of agent process environments."""

import os


GITHUB_TOKEN_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def agent_environment():
    # Preserve provider authentication, SSH_AUTH_SOCK, PATH and user settings.
    # Never mutate the controller environment: subsequent API calls still need it.
    return {key: value for key, value in os.environ.items() if key not in GITHUB_TOKEN_VARIABLES}


def agent_shell_prefix():
    # Herdr appends its canonical executable/argv to the pane's shell input.
    # Apply env at that boundary too: the pane may predate this controller and
    # may have sourced credentials from shell startup files. No values go on argv.
    return "env " + " ".join("-u " + key for key in GITHUB_TOKEN_VARIABLES) + " "
