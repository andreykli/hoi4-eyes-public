"""Public build: internal developer operations are not distributed."""
PUBLIC_BUILD = True
def enabled(root=None):
    return False
def require(root=None):
    raise PermissionError('developer_access_required')
