import pwd

def resolve_username(user_id):
    """convert UID to real username"""
    if user_id is None:
        return ""
    if isinstance(user_id, int) or (isinstance(user_id, str) and str(user_id).isdigit()):
        try:
            return pwd.getpwuid(int(user_id)).pw_name
        except (KeyError, ValueError):
            return str(user_id)
    return str(user_id)
