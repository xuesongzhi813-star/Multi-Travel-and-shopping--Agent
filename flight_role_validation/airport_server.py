"""Read-only sample MCP server for airport codes."""

from typing import Annotated

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field


SERVER_NAME = "travel-airport-demo"
AIRPORT_CODE_PATTERN = "^[A-Z]{3}$"
AIRPORT_INPUT_SCHEMA = {
    "type": "object", "properties": {"code": {"type": "string", "pattern": AIRPORT_CODE_PATTERN}},
    "required": ["code"],
}
CITIES = {"ZRH": "Zürich", "VIE": "Wien"}


class AirportMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: Annotated[str, Field(pattern=AIRPORT_CODE_PATTERN)]
    city: str | None


server = FastMCP(SERVER_NAME)


@server.tool()
def lookup_airport_city(code: Annotated[str, Field(pattern=AIRPORT_CODE_PATTERN)]) -> AirportMatch:
    """Find the city for a sample airport code."""
    return AirportMatch(code=code, city=CITIES.get(code))


if __name__ == "__main__":
    server.run(transport="stdio")
