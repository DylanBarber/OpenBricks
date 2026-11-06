"""Project-authorized access to bounded, persisted run profiles."""

from fastapi import Depends, HTTPException, Request

from .profiling import compare_profiles


def register_profile_routes(app, authenticated, access):
    @app.get("/api/runs/{id}/profile")
    def profile(
        id: str,
        request: Request,
        compare_run_id: str | None = None,
        user=Depends(authenticated),
    ):
        def permitted(identifier):
            run = request.app.state.store.get("runs", identifier)
            if not run:
                raise HTTPException(404, "Run not found")
            access.require_project(user, run.get("project_id", "default"), "read")
            return run

        current = permitted(id)
        response = {
            "run_id": id,
            "notebook_id": current["notebook_id"],
            "status": current["status"],
            "duration_ms": current.get("duration_ms"),
            "profile": current.get("profile"),
            "comparison": None,
        }
        if compare_run_id:
            previous = permitted(compare_run_id)
            if previous["notebook_id"] != current["notebook_id"] or previous.get(
                "project_id", "default"
            ) != current.get("project_id", "default"):
                raise HTTPException(
                    400, "Compare runs from the same notebook and project"
                )
            response["comparison"] = {
                "run_id": compare_run_id,
                "metrics": compare_profiles(current, previous),
            }
        return response
