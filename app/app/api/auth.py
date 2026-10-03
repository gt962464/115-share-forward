from fastapi import APIRouter, Depends, HTTPException, status, File, UploadFile
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from jose import JWTError, jwt
from app.core.database import get_db
from app.models.schema import User as UserModel
from datetime import timedelta
from app.services.auth import (
    verify_password, get_password_hash, create_access_token, 
    SECRET_KEY, ALGORITHM, Token, TokenData, ACCESS_TOKEN_EXPIRE_MINUTES
)

router = APIRouter(prefix="/auth", tags=["auth"])
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
        token_data = TokenData(username=username)
    except JWTError:
        raise credentials_exception
        
    result = await db.execute(select(UserModel).where(UserModel.username == token_data.username))
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user

@router.post("/login", response_model=Token)
async def login(db: AsyncSession = Depends(get_db), form_data: OAuth2PasswordRequestForm = Depends()):
    result = await db.execute(select(UserModel).where(UserModel.username == form_data.username))
    user = result.scalar_one_or_none()
    
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/profile")
async def get_profile(current_user: UserModel = Depends(get_current_user)):
    return {
        "username": current_user.username,
        "avatar_url": current_user.avatar_url,
        "created_at": current_user.created_at
    }

@router.put("/profile")
async def update_profile(
    data: dict, 
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    if "avatar_url" in data:
        current_user.avatar_url = data["avatar_url"]
    
    if "password" in data:
        current_user.hashed_password = get_password_hash(data["password"])
        
    await db.commit()
    return {"status": "success"}

@router.post("/upload_avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: UserModel = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    import base64
    
    # Validate file type
    if not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="File must be an image")
    
    # Validate file size (max 2MB)
    max_size = 2 * 1024 * 1024
    file_content = await file.read()
    if len(file_content) > max_size:
        raise HTTPException(status_code=400, detail="Avatar file too large (max 2MB)")
    
    # Convert to base64 data URI
    try:
        base64_str = base64.b64encode(file_content).decode('utf-8')
        avatar_data_uri = f"data:{file.content_type};base64,{base64_str}"
    except Exception as e:
        logger.error(f"Failed to encode avatar: {e}")
        raise HTTPException(status_code=500, detail="Could not process image")
    
    # Update user avatar (store base64 data URI)
    current_user.avatar_url = avatar_data_uri
    await db.commit()
    
    return {"status": "success", "avatar_url": avatar_data_uri}
