from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import create_engine, select, or_
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Si DATABASE_URL está configurada se usa Supabase/PostgreSQL.
# Sin ella, el proyecto funciona localmente con SQLite.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./gamedex.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


class VideojuegoDB(Base):
    __tablename__ = "videojuegos"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    titulo: Mapped[str] = mapped_column(nullable=False, index=True)
    plataforma: Mapped[str] = mapped_column(nullable=False, default="Sin especificar")
    horas_estimadas: Mapped[int] = mapped_column(nullable=False, default=0)
    puntuacion: Mapped[float] = mapped_column(nullable=False, default=0)
    portada_url: Mapped[Optional[str]] = mapped_column(nullable=True)


class VideojuegoEntrada(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    titulo: str = Field(min_length=1, max_length=160)
    plataforma: str = Field(default="Sin especificar", max_length=80)
    horas_estimadas: int = Field(default=0, ge=0)
    puntuacion: float = Field(default=0, ge=0, le=10)
    portada_url: Optional[str] = None


class VideojuegoActualizacion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    titulo: Optional[str] = Field(default=None, min_length=1, max_length=160)
    plataforma: Optional[str] = Field(default=None, max_length=80)
    horas_estimadas: Optional[int] = Field(default=None, ge=0)
    puntuacion: Optional[float] = Field(default=None, ge=0, le=10)
    portada_url: Optional[str] = None


app = FastAPI(
    title="GameDex API",
    description="API REST para gestionar el catálogo de videojuegos de GameDex.",
    version="1.0.0",
)

# CORS abierto para desarrollo. En producción, cambia '*' por el dominio del frontend.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def iniciar_base_de_datos() -> None:
    Base.metadata.create_all(bind=engine)
    # Importa el juego de ejemplo del JSON solo cuando la tabla está vacía.
    with SessionLocal() as db:
        if db.scalar(select(VideojuegoDB.id).limit(1)) is None:
            json_path = BASE_DIR / "juegos.json"
            if json_path.exists():
                try:
                    contenido = json.loads(json_path.read_text(encoding="utf-8-sig"))
                    juegos = contenido if isinstance(contenido, list) else [contenido]
                    for juego in juegos:
                        if isinstance(juego, dict) and juego.get("titulo"):
                            db.add(VideojuegoDB(
                                titulo=str(juego["titulo"]),
                                plataforma=str(juego.get("plataforma") or "Sin especificar"),
                                horas_estimadas=max(0, int(juego.get("horas_estimadas") or 0)),
                                puntuacion=min(10, max(0, float(juego.get("puntuacion") or 0))),
                                portada_url=juego.get("portada_url"),
                            ))
                    db.commit()
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    db.rollback()


def serializar(juego: VideojuegoDB) -> dict:
    return {
        "id": juego.id,
        "titulo": juego.titulo,
        "plataforma": juego.plataforma,
        "horas_estimadas": juego.horas_estimadas,
        "puntuacion": juego.puntuacion,
        "portada_url": juego.portada_url,
    }


@app.get("/", tags=["Inicio"])
def inicio():
    return {"mensaje": "GameDex API funcionando", "documentacion": "/docs", "endpoints": "/videojuegos"}


@app.get("/health", tags=["Inicio"])
def health():
    return {"estado": "ok"}


@app.get("/videojuegos", tags=["Videojuegos"])
def listar_videojuegos(
    q: Optional[str] = Query(default=None, description="Buscar por título o plataforma"),
    plataforma: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
):
    with SessionLocal() as db:
        consulta = select(VideojuegoDB)
        if q:
            patron = f"%{q.strip()}%"
            consulta = consulta.where(or_(VideojuegoDB.titulo.ilike(patron), VideojuegoDB.plataforma.ilike(patron)))
        if plataforma:
            consulta = consulta.where(VideojuegoDB.plataforma.ilike(f"%{plataforma.strip()}%"))
        juegos = db.scalars(consulta.order_by(VideojuegoDB.id).offset(skip).limit(limit)).all()
        return [serializar(juego) for juego in juegos]


@app.get("/videojuegos/{videojuego_id}", tags=["Videojuegos"])
def obtener_videojuego(videojuego_id: int):
    with SessionLocal() as db:
        juego = db.get(VideojuegoDB, videojuego_id)
        if juego is None:
            raise HTTPException(status_code=404, detail="Videojuego no encontrado")
        return serializar(juego)


@app.post("/videojuegos", status_code=status.HTTP_201_CREATED, tags=["Videojuegos"])
def crear_videojuego(datos: VideojuegoEntrada):
    with SessionLocal() as db:
        juego = VideojuegoDB(**datos.model_dump())
        db.add(juego)
        db.commit()
        db.refresh(juego)
        return serializar(juego)


@app.put("/videojuegos/{videojuego_id}", tags=["Videojuegos"])
def actualizar_videojuego(videojuego_id: int, datos: VideojuegoEntrada):
    with SessionLocal() as db:
        juego = db.get(VideojuegoDB, videojuego_id)
        if juego is None:
            raise HTTPException(status_code=404, detail="Videojuego no encontrado")
        for campo, valor in datos.model_dump().items():
            setattr(juego, campo, valor)
        db.commit()
        db.refresh(juego)
        return serializar(juego)


@app.patch("/videojuegos/{videojuego_id}", tags=["Videojuegos"])
def editar_videojuego(videojuego_id: int, datos: VideojuegoActualizacion):
    cambios = datos.model_dump(exclude_unset=True)
    if not cambios:
        raise HTTPException(status_code=400, detail="Debes enviar al menos un campo para actualizar")
    with SessionLocal() as db:
        juego = db.get(VideojuegoDB, videojuego_id)
        if juego is None:
            raise HTTPException(status_code=404, detail="Videojuego no encontrado")
        for campo, valor in cambios.items():
            setattr(juego, campo, valor)
        db.commit()
        db.refresh(juego)
        return serializar(juego)


@app.delete("/videojuegos/{videojuego_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Videojuegos"])
def eliminar_videojuego(videojuego_id: int):
    with SessionLocal() as db:
        juego = db.get(VideojuegoDB, videojuego_id)
        if juego is None:
            raise HTTPException(status_code=404, detail="Videojuego no encontrado")
        db.delete(juego)
        db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
