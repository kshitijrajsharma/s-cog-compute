import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from io import BytesIO
from typing import List

import httpx
import mercantile
import numpy as np
from aiocache import cached
from fastapi import (
    BackgroundTasks,
    FastAPI,
    HTTPException,
    Query,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from matplotlib import pyplot as plt
from PIL import Image
from rio_tiler.io import COGReader
from shapely.geometry import box, mapping
from starlette.requests import Request

from src.scog_compute.engine import compute as compute_engine

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.mount("/static", StaticFiles(directory="static"), name="static")

templates = Jinja2Templates(directory="templates")
# store websocket connections
connections: List[WebSocket] = []


@app.get("/", response_class=HTMLResponse)
async def read_index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


with open("data/sentinel-2-bands.json") as f:
    sentinel2_assets = json.load(f)


@app.get("/sentinel2-bands")
async def get_sentinel2_bands(
    band: str = Query(None, description="Band name to filter")
):
    if band:
        if band in sentinel2_assets:
            band_data = sentinel2_assets[band]
            filtered_data = {
                "type": band_data.get("type"),
                "title": band_data.get("title"),
                "eo:bands": band_data.get("eo:bands"),
                "gsd": band_data.get("gsd"),
                "raster:bands": band_data.get("raster:bands"),
            }
            return filtered_data
        else:
            raise HTTPException(status_code=404, detail="Band not found")
    else:
        return {key: value["title"] for key, value in sentinel2_assets.items()}


async def fetch_tile(url, x, y, z):
    def read_tile():
        with COGReader(url) as cog:
            tile, _ = cog.tile(x, y, z)
            return tile

    return await asyncio.to_thread(read_tile)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connections.append(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        connections.remove(websocket)


async def notify_progress(message: str):
    for connection in connections:
        await connection.send_text(message)


@app.post("/compute")
async def compute_aoi_over_time(
    background_tasks: BackgroundTasks,
    min_x: float = Query(..., description="Minimum longitude of the bounding box"),
    min_y: float = Query(..., description="Minimum latitude of the bounding box"),
    max_x: float = Query(..., description="Maximum longitude of the bounding box"),
    max_y: float = Query(..., description="Maximum latitude of the bounding box"),
    start_date: str = Query(
        (datetime.now() - timedelta(days=365 * 1)).strftime("%Y-%m-%d"),
        description="Start date in YYYY-MM-DD format (default: 1 years ago)",
    ),
    end_date: str = Query(
        datetime.now().strftime("%Y-%m-%d"),
        description="End date in YYYY-MM-DD format (default: today)",
    ),
    cloud_cover: int = Query(30, description="Cloud cover percentage (default: 30)"),
    formula: str = Query(
        "(band2 - band1) / (band2 + band1)",
        description="Formula for custom band calculation (default: NDVI)",
    ),
    band1: str = Query(
        "red", description="First band for custom calculation (default: red)"
    ),
    band2: str = Query(
        "nir", description="Second band for custom calculation (default: nir)"
    ),
    operation: str = Query(
        "mean", description="Operation for aggregating results (default: mean)"
    ),
    export_band: str = Query(
        "visual", description="Band to export as TIFF and create GIF (default: visual)"
    ),
    output_dir: str = Query(
        "output", description="Output directory for saving results (default: output)"
    ),
):
    bbox = [min_x, min_y, max_x, max_y]
    background_tasks.add_task(
        run_computation,
        bbox,
        start_date,
        end_date,
        cloud_cover,
        formula,
        band1,
        band2,
        operation,
        export_band,
        output_dir,
    )
    return {"message": "Processing started"}


async def run_computation(
    bbox,
    start_date,
    end_date,
    cloud_cover,
    formula,
    band1,
    band2,
    operation,
    export_band,
    output_dir,
):
    await notify_progress("Starting processing...")
    compute_engine(
        bbox=bbox,
        start_date=start_date,
        end_date=end_date,
        cloud_cover=cloud_cover,
        formula=formula,
        band1=band1,
        band2=band2,
        operation=operation,
        export_band=export_band,
        output_dir=output_dir,
    )
    await notify_progress(f"Processing completed. Results saved in {output_dir}")


@app.get("/search")
async def search_images(
    bbox: str = Query(
        ..., description="Bounding box in the format 'west,south,east,north'"
    ),
    cloud_cover: int = Query(30, description="Maximum cloud cover percentage"),
    start_date: str = Query(None, description="Start date in YYYY-MM-DD format"),
    end_date: str = Query(None, description="End date in YYYY-MM-DD format"),
):
    if not start_date:
        start_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    west, south, east, north = map(float, bbox.split(","))
    bbox_polygon = box(west, south, east, north)
    bbox_geojson = mapping(bbox_polygon)

    STAC_API_URL = "https://earth-search.aws.element84.com/v1/search"
    search_params = {
        "collections": ["sentinel-2-l2a"],
        "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": cloud_cover}},
        "intersects": bbox_geojson,
        "limit": 100,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(STAC_API_URL, json=search_params)
    if response.status_code != 200:
        return JSONResponse(
            content={"error": "Error searching STAC API"},
            status_code=500,
        )

    results = response.json()
    return results


@cached(ttl=3600)
async def cached_generate_tile(
    x: int,
    y: int,
    z: int,
    start_date: str,
    end_date: str,
    cloud_cover: int,
    band1: str,
    band2: str,
    formula: str,
) -> bytes:
    tile = mercantile.Tile(x, y, z)
    bbox = mercantile.bounds(tile)
    bbox_polygon = box(bbox.west, bbox.south, bbox.east, bbox.north)
    bbox_geojson = mapping(bbox_polygon)
    STAC_API_URL = "https://earth-search.aws.element84.com/v1/search"
    search_params = {
        "collections": ["sentinel-2-l2a"],
        "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
        "query": {"eo:cloud_cover": {"lt": cloud_cover}},
        "intersects": bbox_geojson,
        "limit": 1,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(STAC_API_URL, json=search_params)
    if response.status_code != 200:
        raise HTTPException(status_code=404, detail="Error searching STAC API")

    results = response.json()
    if not results["features"]:
        raise HTTPException(
            status_code=404, detail="No images found for the given parameters"
        )

    feature = results["features"][0]
    band1_url = feature["assets"][band1]["href"]
    band2_url = feature["assets"][band2]["href"] if band2 else None

    try:
        tasks = [fetch_tile(band1_url, x, y, z)]
        if band2_url:
            tasks.append(fetch_tile(band2_url, x, y, z))

        tiles = await asyncio.gather(*tasks)
        band1 = tiles[0]
        band2 = tiles[1] if band2_url else None
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if band2 is not None:
        # Consider this is single band
        # Perform custom calculation with two bands
        band1 = band1[0].astype(float)
        band2 = band2[0].astype(float)

        result = eval(formula)
        result = np.ma.masked_invalid(result)
        image = apply_colormap(result)
    else:
        inner_bands = band1.shape[0]
        if inner_bands == 1:
            # Single band image
            band1 = band1[0].astype(float)
            result = eval(formula)
            result = np.ma.masked_invalid(result)
            image = apply_colormap(result)
        else:
            # Multi band image
            band1 = band1.transpose(
                1, 2, 0
            )  # Transpose to (256, 256, 3) or (256, 256, 2)
            image = Image.fromarray(band1)

    buffered = BytesIO()
    image.save(buffered, format="PNG")
    image_bytes = buffered.getvalue()

    return image_bytes, feature


@app.get("/tile/{z}/{x}/{y}")
async def get_tile(
    z: int,
    x: int,
    y: int,
    start_date: str = Query(None),
    end_date: str = Query(None),
    cloud_cover: int = Query(30),
    band1: str = Query(
        "visual", description="First band for custom calculation (default: red)"
    ),
    band2: str = Query(
        None, description="Second band for custom calculation (default: nir)"
    ),
    formula: str = Query(
        "band1",
        description="Formula for custom band calculation (example: (band2 - band1) / (band2 + band1) for NDVI)",
    ),
):
    if z < 10 or z > 16:
        return JSONResponse(
            content={"error": "Zoom level must be between 8 and 17"},
            status_code=400,
        )
    if band1 is None:
        return JSONResponse(
            content={"error": "Band1 is required"},
            status_code=400,
        )
    if not start_date:
        start_date = (datetime.now() - timedelta(days=30 * 12)).strftime("%Y-%m-%d")
    if not end_date:
        end_date = datetime.now().strftime("%Y-%m-%d")

    try:
        start_time = time.time()
        image_bytes, feature = await cached_generate_tile(
            x, y, z, start_date, end_date, cloud_cover, band1, band2, formula
        )
        computation_time = time.time() - start_time

        headers = {
            "X-Computation-Time": str(computation_time),
            "X-Image-Date": feature["properties"]["datetime"],
            "X-Cloud-Cover": str(feature["properties"]["eo:cloud_cover"]),
        }

        return Response(content=image_bytes, media_type="image/png", headers=headers)
    except HTTPException as e:
        return JSONResponse(content={"error": e.detail}, status_code=e.status_code)


def apply_colormap(result):
    result_normalized = (result - result.min()) / (result.max() - result.min())
    colormap = plt.get_cmap("RdYlGn")
    result_colored = colormap(result_normalized)
    result_image = (result_colored[:, :, :3] * 255).astype(np.uint8)
    return Image.fromarray(result_image)
