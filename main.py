from __future__ import annotations

import json
import os
import ssl
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import pg8000.dbapi
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, field_validator

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DATABASE_URL = os.getenv("DATABASE_URL")


def _parsear_db_url(url: str) -> dict[str, Any]:
    parsed = urlparse(url)
    query_params = parse_qs(parsed.query)
    sslmode = query_params.get("sslmode", [None])[0]

    ssl_context = None
    if sslmode in {"require", "verify-ca", "verify-full"}:
        ssl_context = ssl.create_default_context()

    return {
        "user": parsed.username,
        "password": parsed.password,
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "database": parsed.path.lstrip("/"),
        "ssl_context": ssl_context,
    }


def _filas_a_dicts(cur: pg8000.dbapi.Cursor) -> list[dict[str, Any]]:
    if not cur.description:
        return []
    columnas = [col[0] for col in cur.description]
    return [dict(zip(columnas, fila)) for fila in cur.fetchall()]


def _fila_a_dict(cur: pg8000.dbapi.Cursor) -> Optional[dict[str, Any]]:
    fila = cur.fetchone()
    if fila is None or not cur.description:
        return None
    columnas = [col[0] for col in cur.description]
    return dict(zip(columnas, fila))


@contextmanager
def get_conn() -> Iterator[pg8000.dbapi.Connection]:
    if not DATABASE_URL:
        raise HTTPException(
            status_code=500,
            detail="Falta DATABASE_URL en las variables de entorno (.env)",
        )
    params = _parsear_db_url(DATABASE_URL)
    try:
        conn = pg8000.dbapi.connect(**params)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"No se pudo conectar a PostgreSQL: {exc}",
        ) from exc

    try:
        yield conn
    finally:
        conn.close()


# --- Modelos Pydantic ---

class VideojuegoEntrada(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    titulo: str = Field(min_length=1, max_length=160)
    plataforma: str = Field(default="Sin especificar", max_length=80)
    horas: int = Field(default=0, ge=0)
    puntuacion: float = Field(default=0, ge=0, le=10)
    imagen_url: Optional[str] = None
    precio: float = Field(default=0, ge=0)

    @field_validator("imagen_url")
    @classmethod
    def limpiar_imagen_url(cls, v: Optional[str]) -> Optional[str]:
        return None if v == "" else v


class VideojuegoActualizacion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    titulo: Optional[str] = Field(default=None, min_length=1, max_length=160)
    plataforma: Optional[str] = Field(default=None, max_length=80)
    horas: Optional[int] = Field(default=None, ge=0)
    puntuacion: Optional[float] = Field(default=None, ge=0, le=10)
    imagen_url: Optional[str] = None
    precio: Optional[float] = Field(default=None, ge=0)

    @field_validator("imagen_url")
    @classmethod
    def limpiar_imagen_url(cls, v: Optional[str]) -> Optional[str]:
        return None if v == "" else v


# --- Inicialización de BD ---

def iniciar_base_de_datos() -> None:
    if not DATABASE_URL:
        return
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS videojuegos (
                    id SERIAL PRIMARY KEY,
                    titulo TEXT NOT NULL,
                    plataforma TEXT NOT NULL DEFAULT 'Sin especificar',
                    horas INTEGER NOT NULL DEFAULT 0,
                    puntuacion REAL NOT NULL DEFAULT 0,
                    imagen_url TEXT,
                    precio REAL NOT NULL DEFAULT 0
                )
                """
            )
            cur.execute("SELECT id FROM videojuegos LIMIT 1")
            if cur.fetchone() is None:
                json_path = BASE_DIR / "juegos.json"
                if json_path.exists():
                    contenido = json.loads(json_path.read_text(encoding="utf-8-sig"))
                    juegos = contenido if isinstance(contenido, list) else [contenido]
                    for juego in juegos:
                        if isinstance(juego, dict) and juego.get("titulo"):
                            cur.execute(
                                """
                                INSERT INTO videojuegos
                                    (titulo, plataforma, horas, puntuacion, imagen_url, precio)
                                VALUES (%s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    str(juego["titulo"]),
                                    str(juego.get("plataforma") or "Sin especificar"),
                                    max(0, int(juego.get("horas") or juego.get("horas_estimadas") or 0)),
                                    min(10.0, max(0.0, float(juego.get("puntuacion") or 0))),
                                    juego.get("imagen_url") or juego.get("portada_url"),
                                    max(0.0, float(juego.get("precio") or 0)),
                                ),
                            )
            cur.close()
            conn.commit()
    except Exception as exc:
        print(f"Aviso al iniciar BD: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    iniciar_base_de_datos()
    yield


# --- Aplicación FastAPI y Configuración CORS ---

app = FastAPI(
    title="GameDex API",
    description="API REST para gestionar el catálogo de videojuegos de GameDex.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# --- Endpoints ---

@app.get("/", tags=["Inicio"])
def inicio():
    return {
        "mensaje": "GameDex API funcionando",
        "documentacion": "/docs",
        "endpoints": "/videojuegos",
    }


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
    sql = """
        SELECT id, titulo, plataforma, horas, puntuacion, imagen_url, precio
        FROM videojuegos
        WHERE 1=1
    """
    params: list[object] = []

    if q:
        patron = f"%{q.strip()}%"
        sql += " AND (titulo ILIKE %s OR plataforma ILIKE %s)"
        params.extend([patron, patron])

    if plataforma:
        sql += " AND plataforma ILIKE %s"
        params.append(f"%{plataforma.strip()}%")

    sql += " ORDER BY id ASC LIMIT %s OFFSET %s"
    params.extend([limit, skip])

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        resultados = _filas_a_dicts(cur)
        cur.close()
        return resultados


@app.get("/videojuegos/{videojuego_id}", tags=["Videojuegos"])
def obtener_videojuego(videojuego_id: int):
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, titulo, plataforma, horas, puntuacion, imagen_url, precio
            FROM videojuegos
            WHERE id = %s
            """,
            (videojuego_id,),
        )
        juego = _fila_a_dict(cur)
        cur.close()
        if juego is None:
            raise HTTPException(status_code=404, detail="Videojuego no encontrado")
        return juego


@app.post("/videojuegos", status_code=status.HTTP_201_CREATED, tags=["Videojuegos"])
def crear_videojuego(datos: VideojuegoEntrada):
    payload = datos.model_dump()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO videojuegos
                (titulo, plataforma, horas, puntuacion, imagen_url, precio)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id, titulo, plataforma, horas, puntuacion, imagen_url, precio
            """,
            (
                payload["titulo"],
                payload["plataforma"],
                payload["horas"],
                payload["puntuacion"],
                payload["imagen_url"],
                payload["precio"],
            ),
        )
        nuevo = _fila_a_dict(cur)
        cur.close()
        conn.commit()
    return nuevo


@app.put("/videojuegos/{videojuego_id}", tags=["Videojuegos"])
def actualizar_videojuego(videojuego_id: int, datos: VideojuegoEntrada):
    payload = datos.model_dump()
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE videojuegos
            SET titulo = %s,
                plataforma = %s,
                horas = %s,
                puntuacion = %s,
                imagen_url = %s,
                precio = %s
            WHERE id = %s
            RETURNING id, titulo, plataforma, horas, puntuacion, imagen_url, precio
            """,
            (
                payload["titulo"],
                payload["plataforma"],
                payload["horas"],
                payload["puntuacion"],
                payload["imagen_url"],
                payload["precio"],
                videojuego_id,
            ),
        )
        actualizado = _fila_a_dict(cur)
        cur.close()
        if actualizado is None:
