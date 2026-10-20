import json
from pathlib import Path


JOBS_PATH = Path("reports/scraping_jobs.json")


def load_jobs():
    if not JOBS_PATH.exists():
        return []

    with JOBS_PATH.open("r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_jobs(jobs):
    JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)

    with JOBS_PATH.open("w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)


def add_job(job):
    jobs = load_jobs()

    existing_ids = {
        item.get("job_id")
        for item in jobs
    }

    if job["job_id"] in existing_ids:
        raise ValueError(
            f"Job already exists: {job['job_id']}"
        )

    jobs.append(job)
    save_jobs(jobs)


def get_job(job_id):
    for job in load_jobs():
        if job.get("job_id") == job_id:
            return job

    return None


def enabled_jobs():
    return [
        job
        for job in load_jobs()
        if job.get("enabled", True)
    ]
