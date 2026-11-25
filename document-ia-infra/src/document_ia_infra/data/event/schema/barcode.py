from datetime import date
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from fr_2ddoc_parser.model.models import VerificationStatus
from pydantic import BaseModel, Field

from document_ia_infra.core.model.typed_generic_model import GenericProperty

__all__ = [
    "Ants2DDoc",
    "BarcodeModel",
    "BarcodePosition",
    "BarcodeType",
    "BarcodeVariant",
    "DataMatrix",
    "QrCode",
    "VerificationStatus",
]


class BarcodeType(str, Enum):
    QR = "QR"
    CODE_128 = "CODE_128"
    EAN_13 = "EAN_13"
    UPC_A = "UPC_A"
    PDF_417 = "PDF_417"
    DATA_MATRIX = "DATA_MATRIX"
    TWO_D_DOC = "2D_DOC"
    AZTEC = "AZTEC"


class BarcodePosition(BaseModel):
    top_left: tuple[int, int]
    top_right: tuple[int, int]
    bottom_right: tuple[int, int]
    bottom_left: tuple[int, int]


class BarcodeModel(BaseModel):
    position: BarcodePosition
    page_number: int


class Ants2DDoc(BarcodeModel):
    type: Literal[BarcodeType.TWO_D_DOC] = BarcodeType.TWO_D_DOC
    position: BarcodePosition
    is_valid: bool
    verification_status: Optional[VerificationStatus] = Field(
        default=None,
        description="Cryptographic and PKI verification status of the 2D-Doc signature.",
    )
    raw_data: Optional[Any] = Field(default=None, json_schema_extra={"x-mask": True})
    typed_data: list[GenericProperty] = Field(
        default=[], json_schema_extra={"x-mask": True}
    )
    issue_date: Optional[date] = Field(default=None)
    ants_type: Optional[str] = Field(default=None)
    doc_type: Optional[str] = Field(default=None)


class QrCode(BarcodeModel):
    type: Literal[BarcodeType.QR] = BarcodeType.QR
    position: BarcodePosition
    raw_data: str = Field(json_schema_extra={"x-mask": True})


class DataMatrix(BarcodeModel):
    type: Literal[BarcodeType.DATA_MATRIX] = BarcodeType.DATA_MATRIX
    position: BarcodePosition
    raw_data: str = Field(json_schema_extra={"x-mask": True})


BarcodeVariant = Annotated[
    Union[Ants2DDoc, QrCode, DataMatrix], Field(discriminator="type")
]
