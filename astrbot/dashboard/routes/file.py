from quart import abort, send_file
import os
from urllib.parse import quote

from astrbot import logger
from astrbot.core import file_token_service

from .route import Route, RouteContext


class FileRoute(Route):
    def __init__(
        self,
        context: RouteContext,
    ) -> None:
        super().__init__(context)
        self.routes = {
            "/file/<file_token>": ("GET", self.serve_file),
        }
        self.register_routes()

    async def serve_file(self, file_token: str):
        try:
            file_path = await file_token_service.handle_file(file_token)
            filename = os.path.basename(file_path)
            
            # Manually set Content-Disposition to handle UTF-8 filenames for iOS
            response = await send_file(file_path, as_attachment=False)
            encoded_filename = quote(filename)
            response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}"
            
            return response
        except (FileNotFoundError, KeyError) as e:
            logger.warning(str(e))
            return abort(404)
