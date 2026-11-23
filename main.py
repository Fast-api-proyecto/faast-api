from fastapi import FastAPI

app = FastAPI(title="API Videojuegos")

@app.get("/")
def inicio():
    return {"mensaje": "API funcionando"}
