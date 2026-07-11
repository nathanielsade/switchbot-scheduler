import logging

log = logging.getLogger("home_agent")


def load_roborock_client(config):
    """Build the real cloud RoborockClient from config, or return None (with a warning) when
    credentials are unset. python-roborock is imported LAZILY inside the real build path (Task 3),
    so importing this module never touches the network."""
    if not config.roborock_username or not config.roborock_password:
        log.warning("ROBOROCK_USERNAME/PASSWORD unset — Roborock control disabled")
        return None
    # Real cloud client build lands in Task 3.
    return _build_cloud_client(config)


def _build_cloud_client(config):  # replaced with the real lazy-import build in Task 3
    raise NotImplementedError
