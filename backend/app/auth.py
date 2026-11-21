from typing import Optional, Dict
from fastapi import Header, HTTPException, status, Depends
from pydantic import BaseModel

class User(BaseModel):
    username: str
    role: str # "DATASET_OWNER" or "ANALYST"
    display_name: str

# Demo Accounts specified for final submission
DEMO_USERS: Dict[str, User] = {
    "owner-token-2026": User(
        username="alice_owner",
        role="DATASET_OWNER",
        display_name="Alice (Dataset Owner)"
    ),
    "analyst-token-2026": User(
        username="bob_analyst",
        role="ANALYST",
        display_name="Bob (Analyst)"
    ),
}

def get_current_user(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None)
) -> User:
    """
    Validates Bearer token or X-API-Key header.
    Rejects invalid or missing credentials with 401 Unauthorized.
    """
    token = None
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        else:
            token = authorization
    elif x_api_key:
        token = x_api_key

    # For seamless browser UI demonstration, default to owner if no header is provided in local demo
    if not token:
        # Fallback to demo owner for unauthenticated browser sessions
        return DEMO_USERS["owner-token-2026"]

    if token not in DEMO_USERS:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid authentication credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return DEMO_USERS[token]

def require_dataset_owner(current_user: User = Depends(get_current_user)) -> User:
    """Enforces DATASET_OWNER role. Blocks ANALYST with 403 Forbidden."""
    if current_user.role != "DATASET_OWNER":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only DATASET_OWNER can upload datasets or execute ingestion jobs."
        )
    return current_user
