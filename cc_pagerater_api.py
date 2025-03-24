import logging
import uvicorn
import sys

from cc_pagerater import get_page_rating, get_config
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Retrieve config
config = get_config()

# Set up templates directory
templates = Jinja2Templates(directory="templates")

# Create FastAPI app
app = FastAPI()

# Pydantic validation
class PageRatingRequest(BaseModel):
    wiki_url: str

# Display Home page
@app.get("/", response_class=HTMLResponse)
async def serve_home(request: Request):
    """Renders the HTML template for the user interface."""
    title = config["webapp_title"]
    return templates.TemplateResponse("index.html", {"request": request, "title": title}) 


# PageRater API
@app.post("/rate-page/")
async def rate_page_api(request: PageRatingRequest):
    """
    API endpoint to fetch and rate the quality of a Confluence page.
    
    Args:
        request (PageRatingRequest): Pydantic model containing wiki_url.

    Returns:
        JSON response with page rating and token usage or an error message.
    """
    try:
        result = get_page_rating(request.wiki_url, config["deployment_name"])

        if "error" in result:
            raise HTTPException(status_code=400, detail=result)

        return result

    except Exception as e:
        logging.exception("Unexpected error in /rate-page API")
        raise HTTPException(status_code=500, detail="Internal Server Error")


# Health API
@app.get("/health")
def health_check():
    return {"status": "ok"}

    
if __name__ == "__main__":
    try:
        # Dynamically get module name or fallback to __main__
        module_name = sys.modules[__name__].__package__ or "__main__"

        # Get configurations from environment variables with defaults
        host = config["webapp_host"]
        port = int(config["webapp_port"])
        
        uvicorn.run(f"{module_name}:app", host=host, port=port)

    except Exception as e:
        logger.error(f"Unexpected error occurred. Please try again later: {e}")