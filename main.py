# main.py
from fastapi import FastAPI, HTTPException, Depends
from fastapi.security import APIKeyHeader
from sqlalchemy import create_engine, Column, String, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from pydantic import BaseModel
import click
import httpx
import pyperclip
import uvicorn
import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Environment validation
SECRET_KEY = os.getenv('SECRET_KEY')
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in environment")

# Configuration
DATABASE_URL = os.getenv('DATABASE_URL', 'sqlite:///noclip.db')
PORT = int(os.getenv('PORT', '8000'))
HOST = os.getenv('HOST', '0.0.0.0')
DEBUG = os.getenv('DEBUG', 'false').lower() == 'true'

# Database setup
Base = declarative_base()
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True)
    api_key = Column(String, unique=True)

class Friendship(Base):
    __tablename__ = 'friendships'
    user_id = Column(String, ForeignKey('users.id'), primary_key=True)
    friend_id = Column(String, ForeignKey('users.id'), primary_key=True)

class Clip(Base):
    __tablename__ = 'clips'
    owner_id = Column(String, ForeignKey('users.id'), primary_key=True)
    bucket = Column(String, primary_key=True)
    content = Column(String)

Base.metadata.create_all(engine)

# FastAPI app
app = FastAPI(title="NoClip", description="Clipboard sharing between machines")

# Auth
api_key_header = APIKeyHeader(name="X-API-Key")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def get_current_user(api_key: str = Depends(api_key_header), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.api_key == api_key).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user

# API Models
class ClipContent(BaseModel):
    content: str

# API Routes
@app.put("/clip/{bucket}")
async def put_clip(bucket: str, content: ClipContent, user = Depends(get_current_user), db: Session = Depends(get_db)):
    clip = db.query(Clip).filter(Clip.owner_id == user.id, Clip.bucket == bucket).first()
    if clip:
        clip.content = content.content
    else:
        clip = Clip(owner_id=user.id, bucket=bucket, content=content.content)
        db.add(clip)
    db.commit()
    return {"status": "success"}

@app.get("/clip/{owner_id}/{bucket}")
async def get_clip(owner_id: str, bucket: str, user = Depends(get_current_user), db: Session = Depends(get_db)):
    friendship = db.query(Friendship).filter(
        ((Friendship.user_id == user.id) & (Friendship.friend_id == owner_id)) |
        ((Friendship.user_id == owner_id) & (Friendship.friend_id == user.id))
    ).first()
    
    if not friendship and user.id != owner_id:
        raise HTTPException(status_code=403, detail="Not authorized to access this clip")
    
    clip = db.query(Clip).filter(Clip.owner_id == owner_id, Clip.bucket == bucket).first()
    if not clip:
        raise HTTPException(status_code=404, detail="Clip not found")
    return {"content": clip.content}

@app.post("/users/add/{friend_id}")
async def add_friend(friend_id: str, user = Depends(get_current_user), db: Session = Depends(get_db)):
    if friend_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot add yourself as friend")
    
    friend = db.query(User).filter(User.id == friend_id).first()
    if not friend:
        raise HTTPException(status_code=404, detail="User not found")
    
    existing_friendship = db.query(Friendship).filter(
        ((Friendship.user_id == user.id) & (Friendship.friend_id == friend_id)) |
        ((Friendship.user_id == friend_id) & (Friendship.friend_id == user.id))
    ).first()
    
    if existing_friendship:
        return {"status": "already friends"}
    
    friendship = Friendship(user_id=user.id, friend_id=friend_id)
    db.add(friendship)
    db.commit()
    return {"status": "success"}

# CLI Configuration
CONFIG_DIR = Path.home() / ".config" / "noclip"
CONFIG_FILE = CONFIG_DIR / "config"

def load_config():
    if not CONFIG_FILE.exists():
        return {}
    
    config = {}
    with open(CONFIG_FILE) as f:
        for line in f:
            key, value = line.strip().split("=", 1)
            config[key] = value
    return config

def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        for key, value in config.items():
            f.write(f"{key}={value}\n")

def get_server_url():
    server_url = os.getenv('NOCLIP_SERVER')
    if not server_url:
        config = load_config()
        server_url = config.get('server_url')
        if not server_url:
            raise click.UsageError(
                "Server URL not set. Please set NOCLIP_SERVER environment variable "
                "or add it to ~/.config/noclip/config"
            )
    return server_url

# CLI
@click.group()
def cli():
    """NoClip - Share clipboards between machines"""
    pass

@cli.command()
@click.argument('user_id')
def register(user_id: str):
    """Register as a new user"""
    api_key = secrets.token_urlsafe(32)
    
    try:
        config = {"user_id": user_id, "api_key": api_key}
        save_config(config)
        
        db = SessionLocal()
        user = User(id=user_id, api_key=api_key)
        db.add(user)
        db.commit()
        
        click.echo(f"Registered successfully! Your API key has been saved.")
    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)

@cli.command()
@click.argument('friend_id')
def add(friend_id: str):
    """Add a friend by their ID"""
    config = load_config()
    if not config:
        click.echo("Please register first using 'noclip register <user_id>'", err=True)
        return
    
    try:
        response = httpx.post(
            f"{get_server_url()}/users/add/{friend_id}",
            headers={"X-API-Key": config["api_key"]}
        )
        response.raise_for_status()
        click.echo(f"Successfully added {friend_id} as friend!")
    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)

@cli.command()
@click.argument('bucket')
@click.argument('content')
def put(bucket: str, content: str):
    """Put content into a bucket"""
    config = load_config()
    if not config:
        click.echo("Please register first using 'noclip register <user_id>'", err=True)
        return
    
    try:
        response = httpx.put(
            f"{get_server_url()}/clip/{bucket}",
            json={"content": content},
            headers={"X-API-Key": config["api_key"]}
        )
        response.raise_for_status()
        click.echo(f"Content stored in bucket '{bucket}'")
    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)

@cli.command()
@click.argument('owner_id')
@click.argument('bucket', default='default')
def get(owner_id: str, bucket: str):
    """Get content from someone's bucket"""
    config = load_config()
    if not config:
        click.echo("Please register first using 'noclip register <user_id>'", err=True)
        return
    
    try:
        response = httpx.get(
            f"{get_server_url()}/clip/{owner_id}/{bucket}",
            headers={"X-API-Key": config["api_key"]}
        )
        response.raise_for_status()
        content = response.json()["content"]
        pyperclip.copy(content)
        click.echo(f"Content from {owner_id}'s bucket '{bucket}' copied to clipboard")
    except Exception as e:
        click.echo(f"Error: {str(e)}", err=True)

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        uvicorn.run(app, host=HOST, port=PORT)
    else:
        cli()