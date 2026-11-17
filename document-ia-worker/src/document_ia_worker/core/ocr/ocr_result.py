from pydantic import BaseModel


class HttpOcrResult(BaseModel):
    success: bool
    content: str
