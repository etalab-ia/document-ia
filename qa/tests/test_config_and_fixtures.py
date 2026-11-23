"""Unit tests for qa.config and qa.fixtures."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from qa.config import (
    ConfigError,
    ProdGuardError,
    load_env_config,
    mask_secret,
    parse_dotenv_file,
    sanitize_headers,
)
from qa.fixtures import (
    FixtureNotFoundError,
    FixturePathTraversalError,
    discover_scenario_files,
    list_synthetic_fixtures,
    resolve_fixture,
    write_synthetic_fixtures,
)


def test_parse_dotenv_file_and_precedence(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env.qa"
    dotenv.write_text(
        (
            "# comment\n"
            "QA_STAGING_API_KEY='dotenv-staging-secret'\n"
            'export QA_STAGING_ADMIN_API_KEY="dotenv-admin-secret"\n'
            "QA_SANDBOX_API_KEY=plain_value # trailing comment\n"
        ),
        encoding="utf-8",
    )

    parsed = parse_dotenv_file(dotenv)
    assert parsed["QA_STAGING_API_KEY"] == "dotenv-staging-secret"
    assert parsed["QA_STAGING_ADMIN_API_KEY"] == "dotenv-admin-secret"
    assert parsed["QA_SANDBOX_API_KEY"] == "plain_value"

    # Existing environ must override .env.qa
    cfg = load_env_config(
        "develop",  # alias for staging
        dotenv_file=dotenv,
        environ={"QA_STAGING_API_KEY": "env-override-secret"},
    )
    assert cfg.name == "staging"
    assert cfg.profile == "develop"
    assert cfg.get_api_key() == "env-override-secret"
    assert cfg.get_admin_api_key() == "dotenv-admin-secret"


def test_prod_guard_blocks_by_default_and_forbids_load_chaos(tmp_path: Path) -> None:
    empty_dotenv = tmp_path / ".env.qa"
    with pytest.raises(ProdGuardError, match="--allow-prod"):
        load_env_config("prod", dotenv_file=empty_dotenv, environ={})

    # Allowed for read/run when --allow-prod is passed
    prod_cfg = load_env_config(
        "prod", allow_prod=True, dotenv_file=empty_dotenv, environ={}
    )
    assert prod_cfg.name == "prod"
    assert prod_cfg.profile == "baseline"
    assert prod_cfg.allow_load is False
    assert prod_cfg.allow_chaos is False

    # Load and chaos stay forbidden even with --allow-prod
    with pytest.raises(ProdGuardError, match="Load testing is forbidden"):
        load_env_config(
            "prod",
            allow_prod=True,
            for_load=True,
            dotenv_file=empty_dotenv,
            environ={},
        )
    with pytest.raises(ProdGuardError, match="Chaos injection is forbidden"):
        load_env_config(
            "prod",
            allow_prod=True,
            for_chaos=True,
            dotenv_file=empty_dotenv,
            environ={},
        )


def test_unknown_env_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Unknown environment"):
        load_env_config("unknown-env", dotenv_file=tmp_path / ".env.qa", environ={})


def test_secret_masking_and_header_sanitization(tmp_path: Path) -> None:
    parts = ["dia", "staging", "2", "ABCDEFGH", "SUPERSECRETBODY1234567890", "ABCD"]
    secret = "_".join(parts)
    cfg = load_env_config(
        "staging",
        dotenv_file=tmp_path / ".env.qa",
        environ={"QA_STAGING_API_KEY": secret},
    )
    assert secret not in repr(cfg)
    assert secret not in str(cfg.masked_summary())
    assert secret not in cfg.model_dump_json()
    assert mask_secret(secret) == "dia_***CD"
    assert mask_secret("short") == "***"
    assert mask_secret(None) == "<unset>"

    sanitized = sanitize_headers(
        {
            "X-API-KEY": secret,
            "Authorization": f"Bearer {secret}",
            "X-RateLimit-Remaining-Minute": "59",
        }
    )
    assert sanitized["X-API-KEY"] == "***REDACTED***"
    assert sanitized["Authorization"] == "***REDACTED***"
    assert sanitized["X-RateLimit-Remaining-Minute"] == "59"


def _verify_png_chunks(raw: bytes) -> tuple[int, int]:
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    pos = 8
    width = 0
    height = 0
    seen_types: list[bytes] = []
    idat_payload = bytearray()

    while pos < len(raw):
        length = struct.unpack(">I", raw[pos : pos + 4])[0]
        chunk_type = raw[pos + 4 : pos + 8]
        data = raw[pos + 8 : pos + 8 + length]
        expected_crc = struct.unpack(">I", raw[pos + 8 + length : pos + 12 + length])[0]
        actual_crc = zlib.crc32(data, zlib.crc32(chunk_type)) & 0xFFFFFFFF
        assert actual_crc == expected_crc
        seen_types.append(chunk_type)
        if chunk_type == b"IHDR":
            width, height = struct.unpack(">II", data[:8])
        elif chunk_type == b"IDAT":
            idat_payload.extend(data)
        pos += 12 + length

    assert seen_types[0] == b"IHDR"
    assert b"IDAT" in seen_types
    assert seen_types[-1] == b"IEND"
    # Verify zlib stream header is valid (decompress small PNG fully; bomb stream valid)
    decompressor = zlib.decompressobj()
    sample = decompressor.decompress(bytes(idat_payload), 1024)
    assert len(sample) > 0
    return width, height


def test_synthetic_pdf_generators_produce_valid_xref_pdfs(tmp_path: Path) -> None:
    for spec_name in (
        "synthetic:minimal_pdf",
        "synthetic:cni_pdf",
        "synthetic:invalid_date_pdf",
        "synthetic:multipage_pdf",
    ):
        fx = resolve_fixture(spec_name, tmp_path)
        assert fx.content_type == "application/pdf"
        assert fx.content.startswith(b"%PDF-1.4\n")
        assert b"\nxref\n" in fx.content
        assert b"\nstartxref\n" in fx.content
        assert fx.content.endswith(b"%%EOF\n")

        # Validate startxref offset actually points to 'xref'
        startxref_idx = fx.content.rfind(b"startxref\n")
        xref_offset_line = fx.content[
            startxref_idx + len(b"startxref\n") :
        ].splitlines()[0]
        xref_offset = int(xref_offset_line.decode("ascii"))
        assert fx.content[xref_offset : xref_offset + 4] == b"xref"

    cni = resolve_fixture("synthetic:cni_pdf", tmp_path)
    assert b"DUPONT" in cni.content
    assert b"14/05/1990" in cni.content

    invalid_date = resolve_fixture("synthetic:invalid_date_pdf", tmp_path)
    assert b"29/02/2026" in invalid_date.content

    multipage = resolve_fixture("synthetic:multipage_pdf", tmp_path)
    assert b"/Count 3" in multipage.content


def test_synthetic_png_and_decompression_bomb_and_fake_pdf(tmp_path: Path) -> None:
    png = resolve_fixture("synthetic:valid_png", tmp_path)
    assert png.content_type == "image/png"
    w, h = _verify_png_chunks(png.content)
    assert (w, h) == (64, 64)

    bomb = resolve_fixture("synthetic:decompression_bomb_png", tmp_path)
    assert bomb.content_type == "image/png"
    assert len(bomb.content) < 50_000, f"Bomb PNG too large: {len(bomb.content)} bytes"
    bw, bh = _verify_png_chunks(bomb.content)
    assert bw * bh > 100_000_000

    fake = resolve_fixture("synthetic:fake_pdf_shell", tmp_path)
    assert fake.name == "fake.pdf"
    assert fake.content_type == "application/pdf"
    assert fake.content.startswith(b"#!/bin/sh\n")

    assert len(list_synthetic_fixtures()) == 7


def test_local_fixture_resolution_confinement_and_discovery(tmp_path: Path) -> None:
    fixtures_dir = tmp_path / "data"
    (fixtures_dir / "cni").mkdir(parents=True)
    doc = fixtures_dir / "cni" / "sample.pdf"
    doc.write_bytes(b"%PDF-1.4\n%%EOF\n")

    resolved = resolve_fixture("cni/sample.pdf", fixtures_dir)
    assert resolved.name == "sample.pdf"
    assert resolved.content_type == "application/pdf"

    with pytest.raises(FixtureNotFoundError):
        resolve_fixture("cni/missing.pdf", fixtures_dir)

    with pytest.raises(FixturePathTraversalError):
        resolve_fixture("../outside.pdf", fixtures_dir)

    with pytest.raises(FixturePathTraversalError):
        resolve_fixture("/etc/passwd", fixtures_dir)

    # Local scenarios discovery
    local_sc_dir = fixtures_dir / "scenarios"
    local_sc_dir.mkdir()
    local_yaml = local_sc_dir / "custom.yaml"
    local_yaml.write_text("scenarios: []\n", encoding="utf-8")

    discovered = discover_scenario_files(fixtures_dir=fixtures_dir)
    assert local_yaml.resolve() in discovered
    assert any(p.name == "00_smoke.yaml" for p in discovered)

    # Write synthetic fixtures command helper
    out_dir = tmp_path / "generated"
    written = write_synthetic_fixtures(out_dir)
    assert len(written) == 7
    assert all(p.is_file() for p in written)


def test_prod_guard_blocks_base_url_override_and_env_var_to_prod(
    tmp_path: Path,
) -> None:
    empty_dotenv = tmp_path / ".env.qa"

    # Bypass via --base-url pointing at prod hostname
    with pytest.raises(ProdGuardError, match="protected"):
        load_env_config(
            "staging",
            base_url_override="https://api.document-ia.beta.gouv.fr",
            dotenv_file=empty_dotenv,
            environ={},
        )

    # Bypass via QA_STAGING_BASE_URL pointing at prod hostname
    with pytest.raises(ProdGuardError, match="protected"):
        load_env_config(
            "staging",
            dotenv_file=empty_dotenv,
            environ={"QA_STAGING_BASE_URL": "https://api.document-ia.beta.gouv.fr"},
        )

    # Load stays forbidden on prod hostname even with --allow-prod
    with pytest.raises(ProdGuardError, match="Load testing is forbidden"):
        load_env_config(
            "staging",
            allow_prod=True,
            for_load=True,
            base_url_override="https://api.document-ia.beta.gouv.fr",
            dotenv_file=empty_dotenv,
            environ={},
        )

    # Invalid URL scheme rejected
    with pytest.raises(ConfigError, match="Invalid base URL"):
        load_env_config(
            "staging",
            base_url_override="file:///etc/passwd",
            dotenv_file=empty_dotenv,
            environ={},
        )

    # Malformed environments.yaml raises ConfigError (not raw yaml.YAMLError)
    bad_envs = tmp_path / "bad_envs.yaml"
    bad_envs.write_text("environments: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Failed to load environments file"):
        load_env_config(
            "staging",
            environments_file=bad_envs,
            dotenv_file=empty_dotenv,
            environ={},
        )


def test_resolve_fixture_and_write_synthetic_block_symlinks(tmp_path: Path) -> None:
    outside_file = tmp_path / "secret.pdf"
    outside_file.write_bytes(b"%PDF-1.4\n%%EOF\n")

    fixtures_dir = tmp_path / "data"
    fixtures_dir.mkdir()
    escape_link = fixtures_dir / "escape.pdf"
    escape_link.symlink_to(outside_file)

    with pytest.raises(FixturePathTraversalError):
        resolve_fixture("escape.pdf", fixtures_dir)

    out_dir = tmp_path / "out_syn"
    out_dir.mkdir()
    symlink_target = out_dir / "minimal_qa.pdf"
    symlink_target.symlink_to(outside_file)

    with pytest.raises(FixturePathTraversalError, match="symlink"):
        write_synthetic_fixtures(out_dir)
