"""Local WP5 gateway; importing this package starts no servers or databases."""


def create_gateway_app(*args, **kwargs):
    from .main import create_gateway_app as create
    return create(*args, **kwargs)


__all__ = ["create_gateway_app"]
