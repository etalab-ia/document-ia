import logging
from typing import Any, Optional

from fr_2ddoc_parser.api import decode_2d_doc
from fr_2ddoc_parser.model.models import Decoded2DDoc, VerificationStatus

from document_ia_infra.core.model.typed_generic_model import GenericProperty
from document_ia_infra.data.event.schema.barcode import (
    Ants2DDoc,
    BarcodeVariant,
    DataMatrix,
)
from document_ia_worker.workflow.step.extract_barcode_data.base_extract_barcode import (
    BarcodeLike,
    BaseExtractBarcode,
)

logger = logging.getLogger(__name__)


class BaseExtractBarcode2DDoc(BaseExtractBarcode):
    """
    Intermediate base class for steps that need to parse 2D-Doc content.
    """

    @staticmethod
    def _resolve_verification_status(
        result: Decoded2DDoc,
    ) -> tuple[VerificationStatus, bool]:
        """Resolve cryptographic verification status and enforce fail-closed validity."""
        verification = getattr(result, "verification", None)
        if verification is None:
            logger.warning(
                "2D-Doc verification result missing; falling back to fail-closed status=%s",
                VerificationStatus.RESOLVER_UNAVAILABLE.value,
            )
            return VerificationStatus.RESOLVER_UNAVAILABLE, False

        raw_status: Any = getattr(verification, "status", None)
        try:
            status = (
                raw_status
                if isinstance(raw_status, VerificationStatus)
                else VerificationStatus(raw_status)
            )
        except ValueError, TypeError:
            logger.warning(
                "Unknown 2D-Doc verification status encountered; falling back to fail-closed status=%s",
                VerificationStatus.RESOLVER_UNAVAILABLE.value,
            )
            return VerificationStatus.RESOLVER_UNAVAILABLE, False

        raw_result_valid = bool(getattr(result, "is_valid", False))
        raw_verification_valid = bool(getattr(verification, "is_valid", False))

        if status == VerificationStatus.VERIFIED and not (
            raw_result_valid and raw_verification_valid
        ):
            logger.warning(
                "Inconsistent 2D-Doc verification state (status=%s with is_valid mismatch); "
                "falling back to fail-closed status=%s",
                status.value,
                VerificationStatus.RESOLVER_UNAVAILABLE.value,
            )
            return VerificationStatus.RESOLVER_UNAVAILABLE, False

        is_valid = bool(
            raw_result_valid
            and raw_verification_valid
            and status == VerificationStatus.VERIFIED
        )
        if (raw_result_valid or raw_verification_valid) and not is_valid:
            logger.warning(
                "Inconsistent 2D-Doc verification state (is_valid=True with status=%s); forcing is_valid=False",
                status.value,
            )

        return status, is_valid

    def _decode_barcode_to_variant(
        self,
        barcode: BarcodeLike,
        page_number: int,
    ) -> tuple[Optional[BarcodeVariant], Optional[str], bool]:
        """
        Specialized decoding for 2DDoc format.
        """
        format_name = getattr(barcode.format, "name", str(barcode.format))

        if format_name in ("DataMatrix", "BarcodeFormat.DataMatrix", "Data Matrix"):
            key = f"dm:{barcode.text}"
            try:
                result = decode_2d_doc(barcode.text)
                verification_status, is_valid = self._resolve_verification_status(
                    result
                )
                return (
                    Ants2DDoc(
                        position=self._map_position_like_to_model(barcode.position),
                        is_valid=is_valid,
                        verification_status=verification_status,
                        raw_data=result.fields
                        if len(result.fields) > 0
                        else result.typed,
                        typed_data=[]
                        if result.ants_type is None
                        else GenericProperty.convert_pydantic_model(result.typed),
                        page_number=page_number,
                        ants_type=result.ants_type,
                        issue_date=result.header.issue_date,
                        doc_type=result.header.doc_type,
                    ),
                    key,
                    True,
                )
            except Exception as e:
                logger.warning("Failed to decode 2D-Doc barcode: %s", type(e).__name__)
                return (
                    DataMatrix(
                        position=self._map_position_like_to_model(barcode.position),
                        raw_data=barcode.text,
                        page_number=page_number,
                    ),
                    key,
                    False,
                )

        return super()._decode_barcode_to_variant(barcode, page_number)
