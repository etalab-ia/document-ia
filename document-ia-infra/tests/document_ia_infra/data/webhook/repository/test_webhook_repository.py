import uuid

import pytest

from document_ia_infra.data.organization.enum.platform_role import PlatformRole
from document_ia_infra.data.organization.repository.organization_repository import (
    OrganizationRepository,
)
from document_ia_infra.data.webhook.repository.webhook_repository import (
    WebHookRepository,
)


@pytest.mark.asyncio
class TestWebHookRepository:
    async def test_create_list_delete_webhook(self, isolated_database_manager):
        # Create an organization to attach webhooks to
        async with isolated_database_manager.local_session() as session:
            org_repo = OrganizationRepository(session)
            webhook_repo = WebHookRepository(session)
            created_webhook_id = None
            created_org_id = None
            try:
                org = await org_repo.create(
                    contact_email=f"org-{uuid.uuid4().hex[:8]}@example.com",
                    name=f"Org {uuid.uuid4().hex[:6]}",
                    platform_role=PlatformRole.STANDARD,
                )
                await session.commit()
                created_org_id = org.id

                # Create a webhook
                dto = await webhook_repo.create(
                    organization_id=created_org_id,
                    url="https://example.com/webhook",
                    headers={"X-Signature": "abc123", "X-Env": "test"},
                )
                await session.commit()
                created_webhook_id = dto.id

                assert str(dto.organization_id) == str(created_org_id)
                assert dto.url == "https://example.com/webhook"
                assert dto.headers.get("X-Signature") == "abc123"

                # List webhooks for this organization
                items = await webhook_repo.list_webhooks_by_organization(created_org_id)
                assert isinstance(items, list)
                assert any(x.id == created_webhook_id for x in items)

                # Delete the webhook
                deleted = await webhook_repo.delete(created_webhook_id)
                await session.commit()
                assert deleted is True

                # Verify list is now empty
                items_after = await webhook_repo.list_webhooks_by_organization(
                    created_org_id
                )
                assert all(x.id != created_webhook_id for x in items_after)
            finally:
                # Cleanup organization best-effort in fresh session
                if created_org_id is not None:
                    async with (
                        isolated_database_manager.local_session() as clean_session
                    ):
                        clean_org_repo = OrganizationRepository(clean_session)
                        try:
                            await clean_org_repo.delete(created_org_id)
                            await clean_session.commit()
                        except Exception:
                            await clean_session.rollback()

    async def test_delete_nonexistent_returns_false(self, isolated_database_manager):
        # Ensure delete returns False when webhook does not exist
        async with isolated_database_manager.local_session() as session:
            repo = WebHookRepository(session)
            result = await repo.delete(uuid.uuid4())
            assert result is False

    async def test_delete_scoped_to_organization_id(self, isolated_database_manager):
        async with isolated_database_manager.local_session() as session:
            org_repo = OrganizationRepository(session)
            webhook_repo = WebHookRepository(session)
            created_org_ids: list[uuid.UUID] = []
            try:
                org_a = await org_repo.create(
                    contact_email=f"org-a-{uuid.uuid4().hex[:8]}@example.com",
                    name=f"Org A {uuid.uuid4().hex[:6]}",
                    platform_role=PlatformRole.STANDARD,
                )
                org_b = await org_repo.create(
                    contact_email=f"org-b-{uuid.uuid4().hex[:8]}@example.com",
                    name=f"Org B {uuid.uuid4().hex[:6]}",
                    platform_role=PlatformRole.STANDARD,
                )
                await session.commit()
                created_org_ids.extend([org_a.id, org_b.id])

                dto = await webhook_repo.create(
                    organization_id=org_a.id,
                    url="https://example.com/webhook-scoped",
                    headers={"X-Env": "test"},
                )
                await session.commit()

                # Deleting with wrong organization_id must fail and keep the webhook
                deleted_wrong_org = await webhook_repo.delete(
                    dto.id, organization_id=org_b.id
                )
                await session.commit()
                assert deleted_wrong_org is False
                assert await webhook_repo.get_by_id(dto.id) is not None

                # Deleting with matching organization_id succeeds
                deleted_right_org = await webhook_repo.delete(
                    dto.id, organization_id=org_a.id
                )
                await session.commit()
                assert deleted_right_org is True
                assert await webhook_repo.get_by_id(dto.id) is None
            finally:
                for org_id in created_org_ids:
                    async with (
                        isolated_database_manager.local_session() as clean_session
                    ):
                        clean_org_repo = OrganizationRepository(clean_session)
                        try:
                            await clean_org_repo.delete(org_id)
                            await clean_session.commit()
                        except Exception:
                            await clean_session.rollback()
