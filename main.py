from __future__ import annotations
import json
import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Iterator, Optional

import psycopg
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Response, status
from fastapi.middleware.cors import CORSMiddleware
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def obtener_database_url() -> Optional[str]:
    url = os.getenv("DATABASE_URL")
    if url and url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = obtener_database_url()
pool: Optional[ConnectionPool] = None


@contextmanager
def get_conn() -> Iterator[psycopg.Connection]:
    if pool is None:
        raise HTTPException(
            status_code=500,
            detail="La conexión a la base de datos no está configurada (.env)",
        )
    try:
        with pool.connection() as conn:
            yield conn
    except psycopg.OperationalError as exc:
        raise HTTPException(
            status_code=503,
            detail="Error de comunicación con la base de datos",
        ) from exc


# --- Modelos Pydantic ---

class VideojuegoBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    titulo: str = Field(min_length=1, max_length=160)
    plataforma: str = Field(default="Sin especificar", max_length=80)
    horas: int = Field(default=0, ge=0)
    puntuacion: float = Field(default=0, ge=0, le=10)
    imagen_url: Optional[HttpUrl] = None
    precio: float = Field(default=0, ge=0)


class VideojuegoEntrada(VideojuegoBase):
    pass


class VideojuegoActualizacion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    titulo: Optional[str] = Field(default=None, min_length=1, max_length=160)
    plataforma: Optional[str] = Field(default=None, max_length=80)
    horas: Optional[int] = Field(default=None, ge=0)
    puntuacion: Optional[float] = Field(default=None, ge=0, le=10)
    imagen_url: Optional[HttpUrl] = None
    precio: Optional[float] = Field(default=None, ge=0)


class VideojuegoSalida(BaseModel):
    id: int
    titulo: str
    plataforma: str
    horas: int
    puntuacion: float
    imagen_url: Optional[str] = None
    precio: float


# --- Inicialización de BD y Ciclo de Vida ---

def inicializar_esquema_y_datos() -> None:
    if pool is None:
        return

    with pool.connection() as conn:
        with conn.cursor() as cur:
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
            cur.execute("SELECT 1 FROM videojuegos LIMIT 1")
            if cur.fetchone() is None:
                cargar_datos_iniciales(cur)
        conn.commit()


def cargar_datos_iniciales(cur: psycopg.Cursor) -> None:
    json_path = BASE_DIR / "juegos.json"
    if not json_path.exists():
        return

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
                    str(juego["titulo"]).strip(),
                    str(juego.get("plataforma") or "Sin especificar").strip(),
                    max(0, int(juego.get("horas") or juego.get("horas_estimadas") or 0)),
                    min(10.0, max(0.0, float(juego.get("puntuacion") or 0))),
                    juego.get("imagen_url") or juego.get("portada_url"),
                    max(0.0, float(juego.get("precio") or 0)),
                ),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    if DATABASE_URL:
        try:
            pool = ConnectionPool(
                conninfo=DATABASE_URL,
                min_size=1,
                max_size=10,
                kwargs={"row_factory": dict_row},
            )
            inicializar_esquema_y_datos()
        except Exception as exc:
            print(f"Aviso al iniciar el pool o la BD: {exc}")
    yield
    if pool is not None:
        pool.close()


# --- Aplicación FastAPI ---

app = FastAPI(
    title="GameDex API",
    description="API REST para gestionar el catálogo de videojuegos de GameDex.",
    version="1.1.0",
    lifespan=lifespan,
)

# En producción, reemplaza "*" por los dominios permitidos (ej. ["https://tu-web.com"])
ORIGINS = os.getenv("CORS_ORIGINS", "*").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in ORIGINS],
    allow_methods=["*"],
    allow_headers=["*"],
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
    db_conectada = False
    if pool is not None:
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
                db_conectada = True
        except Exception:
            db_conectada = False

    return {
        "estado": "ok" if db_conectada else "degradado",
        "base_de_datos": "conectada" if db_conectada else "desconectada",
    }


@app.get("/videojuegos", response_model=list[VideojuegoSalida], tags=["Videojuegos"])
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
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


@app.get(
    "/videojuegos/{videojuego_id}",
    response_model=VideojuegoSalida,
    tags=["Videojuegos"],
)
def obtener_videojuego(videojuego_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, titulo, plataforma, horas, puntuacion, imagen_url, precio
                FROM videojuegos
                WHERE id = %s
                """,
                (videojuego_id,),
            )
            juego = cur.fetchone()
            if juego is None:
                raise HTTPException(status_code=404, detail="Videojuego no encontrado")
            return juego


@app.post(
    "/videojuegos",
    response_model=VideojuegoSalida,
    status_code=status.HTTP_201_CREATED,
    tags=["Videojuegos"],
)
def crear_videojuego(datos: VideojuegoEntrada):
    payload = datos.model_dump()
    imagen_url = str(payload["imagen_url"]) if payload["imagen_url"] else None

    with get_conn() as conn:
        with conn.cursor() as cur:
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
                    imagen_url,
                    payload["precio"],
                ),
            )
            nuevo = cur.fetchone()
        conn.commit()
    return nuevo


@app.put(
    "/videojuegos/{videojuego_id}",
    response_model=VideojuegoSalida,
    tags=["Videojuegos"],
)
def actualizar_videojuego(videojuego_id: int, datos: VideojuegoEntrada):
    payload = datos.model_dump()
    imagen_url = str(payload["imagen_url"]) if payload["imagen_url"] else None

    with get_conn() as conn:
        with conn.cursor() as cur:
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
                    imagen_url,
                    payload["precio"],
                    videojuego_id,
                ),
            )
            actualizado = cur.fetchone()
            if actualizado is None:
