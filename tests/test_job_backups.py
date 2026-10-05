from dataclasses import replace
from unittest.mock import Mock

from mn_api import state
from mn_api.routes.v1 import job_backups
from mn_sdk.job_backup import BackupRestoreError


def test_backup_download_and_restore_without_catalog(api_client, monkeypatch):
    def backup(client, job_id, output, **kwargs):
        assert client is state.client
        assert job_id == "job-source"
        assert kwargs == {"air_gapped": True}
        output.write_bytes(b"PK-backup")
    monkeypatch.setattr(job_backups, "backup_job", backup)
    paths = []
    def restore(client, input_path, *, start):
        assert input_path.read_bytes() == b"PK-backup"
        assert start is True
        paths.append(input_path)
        return {"job_id": "job-new", "run_id": "run-new", "started": True}
    monkeypatch.setattr(job_backups, "restore_job", restore)
    download = api_client.post("/api/v1/jobs/job-source/backups")
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    restored = api_client.post("/api/v1/job-restorations?start=true", content=download.content, headers={"content-type": "application/zip"})
    assert restored.status_code == 201
    assert restored.json()["job_id"] == "job-new"
    assert not paths[0].parent.exists()


def test_bad_type_bad_capsule_and_auth_are_structured(api_client, monkeypatch, state_snapshot):
    assert api_client.post("/api/v1/job-restorations", content=b"bad").status_code == 415
    monkeypatch.setattr(job_backups, "restore_job", Mock(side_effect=BackupRestoreError("Backup inventory or checksum mismatch")))
    response = api_client.post("/api/v1/job-restorations", content=b"bad", headers={"content-type": "application/zip"})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    monkeypatch.setattr(state, "config", replace(state.config, api_token="private-token"))
    assert api_client.post("/api/v1/jobs/job-source/backups").status_code == 401
    assert api_client.post("/api/v1/job-restorations", content=b"bad", headers={"content-type": "application/zip"}).status_code == 401


def test_backup_failure_and_upload_cap(api_client, monkeypatch):
    monkeypatch.setattr(job_backups, "backup_job", Mock(side_effect=BackupRestoreError("Required model asset is missing")))
    assert api_client.post("/api/v1/jobs/job-source/backups").status_code == 422
    monkeypatch.setattr(job_backups, "MAX_BYTES", 2)
    assert api_client.post("/api/v1/job-restorations", content=b"zip", headers={"content-type": "application/zip"}).status_code == 413
