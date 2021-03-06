import argparse
import asyncio
import logging
import os
import sys

from .logs import file_callback_log
from .logs import websocket_log

logger = logging.getLogger(__name__)

class ContextLogRecord(logging.LogRecord):
    no_context_found = "NoContext"
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        task = asyncio.Task.current_task()
        if task is not None:
            self.log_context = getattr(task, "log_context", self.no_context_found)
        else:
            self.log_context = self.no_context_found

def override(config, config_coords, args, arg_name):
    if getattr(args, arg_name, None) is not None:
        def resolve_leaf_dict(parent_dict, leaf_coords):
            if len(leaf_coords) == 1:
                return parent_dict
            return resolve_leaf_dict(parent_dict[leaf_coords[0]], leaf_coords[1:])
        d = resolve_leaf_dict(config, config_coords)
        logger.debug("Overriding config/{} with arg {}".format("/".join(config_coords), arg_name))
        d[config_coords[-1]] = getattr(args, arg_name)

#
# Subcommands
#

def run_subcommand(args):
    from .server import server

    # Config
    config = load_config(args.config)
    override(config, ("log", "path"), args, "log")
    override(config, ("bind", "address"), args, "address")
    override(config, ("bind", "port"), args, "port")

    # Logging
    log_default_level = logging._nameToLevel[config["log"]["level"]]
    configure_logging(log_default_level, config["log"]["path"], args.verbose, args.quiet, args.silent)

    # Mode B
    if args.mode_b:
        repo_provider = {
            "type": "modeb",
            "params": {},
        }
    else:
        repo_provider = config["repo_provider"]

    # Go
    server.start_server(
        bind=config["bind"],
        repo_provider=repo_provider,
        adjust_provider=config["adjust_provider"],
    )

def run_container_subcommand(args):
    from .server import server

    # Log to stdout/stderr only (no file)
    configure_logging(logging.INFO)

    # Read required config from env vars, most of it is hardcoded though
    missing_envs = []
    def required_env(name, desc):
        val = os.environ.get(name, None)
        # Val should not be empty or None
        if not val:
            missing_envs.append((name, desc))
        return val
    da_url = required_env("REPOUR_PME_DA_URL", "The REST endpoint required by PME to look up GAVs")
    repour_url = required_env("REPOUR_URL", "Repour's URL")

    # Mode B
    if args.mode_b:
        # ssh_user = required_env("REPOUR_SSH_USER", "The SSH username expected by the git server")
        # os.makedirs(".ssh", mode=0o700, exist_ok=True)
        # with open(".ssh/config", "w") as f:
        #     f.write("Host *\n\tUser {ssh_user}\n".format(**locals()))
        # os.chmod(".ssh/config", 0o600)
        repo_provider = {
            "type": "modeb",
            "params": {},
        }
    else:
        # gitolite uses ssh key auth, will be mounted as OpenShift Secret file in secrets/gitolite/repour.key
        # https://github.com/kubernetes/kubernetes/blob/master/docs/design/secrets.md#use-case-pod-with-ssh-keys
        gitolite_host = required_env("REPOUR_GITOLITE_HOST", "The hostname of the repository provider")

    if missing_envs:
        print("Missing environment variable(s):")
        for missing_env in missing_envs:
            print("{m[0]} ({m[1]})".format(m=missing_env))
        return 2

    if not args.mode_b:
        # Read optional env vars
        gitolite_ssh_port = os.environ.get("REPOUR_GITOLITE_SSH_PORT", "2222")
        gitolite_ssh_user = os.environ.get("REPOUR_GITOLITE_SSH_USER", "git")
        gitolite_http_port = os.environ.get("REPOUR_GITOLITE_HTTP_PORT", "8080")
        gitolite_user = os.environ.get("REPOUR_GITOLITE_USER", "repour")

        gitolite_ssh_url = "ssh://{ssh_user}@{host}:{port}/{user}".format(
            host=gitolite_host,
            port=gitolite_ssh_port,
            ssh_user=gitolite_ssh_user,
            user=gitolite_user,
        )
        gitolite_http_url = "http://{host}:{port}/{user}".format(
            host=gitolite_host,
            port=gitolite_http_port,
            user=gitolite_user,
        )

        repo_provider = {
            "type": "gitolite",
            "params": {
                "ssh_url": gitolite_ssh_url,
                "http_url": gitolite_http_url,
            },
        }

    # Go
    server.start_server(
        bind={
            "address": None,
            "port": 7331,
        },
        repo_provider = repo_provider,
        repour_url = repour_url,
        adjust_provider = {
            "type": "subprocess",
            "params": {
                "description": "PME",
                "cmd": [
                    "java", "-jar", os.path.join(os.getcwd(), "pom-manipulation-cli.jar"),
                    "-s", "/home/repour/settings.xml",
                    "-d",
                    "-DrestMaxSize=30",
                    "-DrestURL=" + da_url,
                    "-Dversion.incremental.suffix=redhat",
                    "-DstrictAlignment=true",
                    "-DoverrideTransitive=false",
                    "-DallowConfigFilePrecedence=true",
                    "-DrestProtocol=current"
                ],
                "log_context_option": "--log-context",
                "send_log": False, # enable when PNC central logging is ready
            },
        },
    )

#
# General
#

def create_argparser():
    parser = argparse.ArgumentParser(description="Run repour server in various modes")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase logging verbosity one level, repeatable.")
    parser.add_argument("-q", "--quiet", action="count", default=0, help="Decrease logging verbosity one level, repeatable.")
    parser.add_argument("-s", "--silent", action="store_true", help="Do not log to stdio.")
    parser.add_argument("-l", "--log", help="Override the path for the log file provided in the config file.")

    subparsers = parser.add_subparsers()

    run_desc = "Run the server"
    run_parser = subparsers.add_parser("run", help=run_desc)
    run_parser.description = run_desc
    run_parser.set_defaults(func=run_subcommand)
    run_parser.add_argument("-c", "--config", default="config.yaml", help="Path to the configuration file. Default: config.yaml")
    run_parser.add_argument("-a", "--address", help="Override the bind IP address provided in the config file.")
    run_parser.add_argument("-p", "--port", help="Override the bind port number provided in the config file.")
    run_parser.add_argument("--mode-b", action="store_true", help="Run the server with client-specified internal repositories")

    run_container_desc = "Run the server in a container environment"
    run_container_parser = subparsers.add_parser("run-container", help=run_container_desc)
    run_container_parser.description = run_container_desc
    run_container_parser.set_defaults(func=run_container_subcommand)
    run_container_parser.add_argument("--mode-b", action="store_true", help="Run the server with client-specified internal repositories")

    return parser

def configure_logging(default_level, log_path=None, verbose_count=0, quiet_count=0, silent=False):
    logging.setLogRecordFactory(ContextLogRecord)

    formatter = logging.Formatter(
        fmt="{asctime} [{levelname}] [{log_context}] {name}:{lineno} {message}",
        style="{",
    )

    formatter_callback = logging.Formatter(
        fmt="[{levelname}] {name}:{lineno} {message}",
        style="{",
    )

    root_logger = logging.getLogger()

    if log_path is not None:
        file_log = logging.FileHandler(log_path)
        file_log.setFormatter(formatter)
        root_logger.addHandler(file_log)

    if not silent:
        console_log = logging.StreamHandler()
        console_log.setFormatter(formatter)
        root_logger.addHandler(console_log)

    ws_log = websocket_log.WebsocketLoggerHandler()
    ws_log.setFormatter(formatter_callback)
    root_logger.addHandler(ws_log)

    callback_id_log = file_callback_log.FileCallbackHandler()
    callback_id_log.setFormatter(formatter_callback)
    root_logger.addHandler(callback_id_log)

    log_level = default_level + (10 * quiet_count) - (10 * verbose_count)
    root_logger.setLevel(log_level)

def load_config(config_path):
    import yaml
    from . import validation

    config_dir = os.path.dirname(config_path)
    def config_relative(loader, node):
        value = loader.construct_scalar(node)
        return os.path.abspath(os.path.join(config_dir, value))
    yaml.add_constructor("!config_relative", config_relative)

    with open(config_path, "r") as f:
        config = yaml.load(f)

    return validation.server_config(config)

def main():
    # Args
    parser = create_argparser()
    args = parser.parse_args()

    if "func" in args:
        sys.exit(args.func(args))
    else:
        parser.print_help()
        sys.exit(1)
