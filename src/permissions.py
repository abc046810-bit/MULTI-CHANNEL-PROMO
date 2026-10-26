"""Role model. Every privileged action must pass through these checks on the backend."""
from enum import IntEnum


class Role(IntEnum):
    NONE = 0
    ADMIN = 1
    OWNER = 2


def role_for(user_id: int, owner_id: int, admin_ids: set[int]) -> Role:
    if user_id == owner_id:
        return Role.OWNER
    if user_id in admin_ids:
        return Role.ADMIN
    return Role.NONE


def is_allowed(role: Role, minimum: Role) -> bool:
    return role >= minimum


def check_admin_change(actor: Role, target_id: int, owner_id: int) -> None:
    if actor != Role.OWNER:
        raise PermissionError("Only the owner can manage administrators.")
    if target_id == owner_id:
        raise PermissionError("The owner cannot be added or removed as an administrator.")
