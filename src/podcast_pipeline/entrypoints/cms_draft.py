from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import typer

from podcast_pipeline.cms_api import (
    AUDIOS_PATH,
    EPISODE_DETAIL_PATH,
    EPISODES_PATH,
    PARENTS_PATH,
    POST_LOOKUP_PATH,
    CmsApiError,
    DjangoCastClient,
    audio_file_label,
    load_cms_token,
)
from podcast_pipeline.cms_draft import (
    AUDIO_ID_PLACEHOLDER,
    CmsConfig,
    CmsConfigError,
    CmsDraftPlan,
    build_cms_draft_plan,
    load_cms_config,
    selected_slug,
)
from podcast_pipeline.workspace_store import EpisodeWorkspaceStore, atomic_write_text

_EPISODE_TYPE = "cast.Episode"
_PODCAST_TYPE = "cast.Podcast"
_DIARIZATION_LABELS = {"enabled": "Enabled", "disabled": "Disabled", "inherit": "Inherit"}


@dataclass(frozen=True)
class CmsDraftState:
    """What ``cms/draft.json`` remembers between runs, so a re-run updates instead of duplicating."""

    base_url: str
    episode_id: int | None = None
    audio_id: int | None = None
    audio_edit_url: str | None = None
    audio_attached: bool = True
    """False between uploading an Audio and the episode request that attaches it."""
    transcript_pending: bool = False
    """An uploaded Audio whose transcript was neither started nor handed to the user as a manual step yet."""


@dataclass
class _DraftRun:
    """Per-run context: the client, the workspace state and the manual steps that remain, in order."""

    client: DjangoCastClient
    store: EpisodeWorkspaceStore
    config: CmsConfig
    plan: CmsDraftPlan
    state: CmsDraftState
    steps: list[str]
    _audio_patch_supported: bool | None = None

    def save(self, **changes: Any) -> None:
        self.state = replace(self.state, **changes)
        _write_state(self.store, self.state)

    def audio_patch_supported(self, audio_id: int) -> bool:
        if self._audio_patch_supported is None:
            self._audio_patch_supported = self.client.supports_adding_audio_files(audio_id)
        return self._audio_patch_supported

    def audio_editor(self, audio_id: int) -> str:
        url = self.state.audio_edit_url if self.state.audio_id == audio_id else None
        return url or f"the Wagtail audio editor (Audio {audio_id})"


def run_cms_draft(*, workspace: Path, dry_run: bool, new_audio: bool = False, transcript: bool = False) -> None:
    store = EpisodeWorkspaceStore(workspace)
    config, plan = _load_plan(store, workspace)
    if dry_run:
        typer.echo(json.dumps(_describe_plan(config, plan), indent=2, ensure_ascii=False))
        return
    try:
        token = load_cms_token(config.token_env)
        with _cms_lock(store), DjangoCastClient(config.base_url, token) as client:
            run = _DraftRun(client, store, config, plan, _read_state(store, config), steps=[])
            _create_or_update(run, new_audio=new_audio, transcript=transcript)
    except CmsApiError as exc:
        raise typer.BadParameter(str(exc)) from exc


def run_cms_publish(*, workspace: Path, yes: bool) -> None:
    store = EpisodeWorkspaceStore(workspace)
    episode_yaml = _read_episode_yaml(store, workspace)
    try:
        config = load_cms_config(episode_yaml)
        slug = selected_slug(workspace)
    except CmsConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    try:
        token = load_cms_token(config.token_env)
        with _cms_lock(store), DjangoCastClient(config.base_url, token) as client:
            _publish(client, store, config, slug=slug, yes=yes)
    except CmsApiError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _read_episode_yaml(store: EpisodeWorkspaceStore, workspace: Path) -> dict[str, Any]:
    if not store.layout.episode_yaml.exists():
        raise typer.BadParameter(f"Missing episode.yaml in {workspace}")
    return store.read_episode_yaml()


def _load_plan(store: EpisodeWorkspaceStore, workspace: Path) -> tuple[CmsConfig, CmsDraftPlan]:
    episode_yaml = _read_episode_yaml(store, workspace)
    try:
        config = load_cms_config(episode_yaml)
        plan = build_cms_draft_plan(episode_yaml=episode_yaml, workspace=workspace, config=config)
    except CmsConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return config, plan


def _describe_plan(config: CmsConfig, plan: CmsDraftPlan) -> dict[str, Any]:
    parent: int | str = config.parent_id if config.parent_id is not None else f"<resolved from GET {PARENTS_PATH}>"
    return {
        "base_url": config.base_url,
        "token_env": config.token_env,
        "lookup": {"path": POST_LOOKUP_PATH, "params": {"parent": parent, "slug": plan.slug}},
        "audio_upload": {
            "path": AUDIOS_PATH,
            "file": {plan.audio.file.field: str(plan.audio.file.path)},
            "fields": plan.audio.form_fields(),
        },
        "extra_audio_files": {item.field: str(item.path) for item in plan.extra_audio_files},
        "episode_create": {"path": EPISODES_PATH, "body": {"parent": {"id": parent}, **_dry_run_episode(plan)}},
        "episode_update": {
            "path": EPISODE_DETAIL_PATH,
            "body": {
                **_dry_run_episode(plan),
                "base_revision_id": "<latest_revision_id>",
                "require_unpublished": True,
            },
        },
        "contributors": plan.contributors_payload(),
        "hosts_without_contributor": list(plan.unmapped_hosts),
    }


def _dry_run_episode(plan: CmsDraftPlan) -> dict[str, Any]:
    return plan.episode_payload(AUDIO_ID_PLACEHOLDER)


def _create_or_update(run: _DraftRun, *, new_audio: bool, transcript: bool) -> None:
    client, plan = run.client, run.plan
    parent_id = _resolve_parent(client, run.config)
    existing = _find_existing_episode(client, run.state, parent_id=parent_id, slug=plan.slug)
    if existing is not None:
        _refuse_live(existing)

    audio_id, audio = _audio_for_draft(run, existing, new_audio=new_audio)
    _add_missing_formats(run, audio_id, audio)
    diarization_ready = _ensure_diarization(run, audio_id, audio)

    previous_audio_id = _podcast_audio_id(existing)
    payload = plan.episode_payload(audio_id)
    if existing is None:
        episode = client.create_episode({"parent": {"id": parent_id}, **payload})
        action = "Created"
    else:
        episode = client.update_episode(
            int(existing["id"]), payload, base_revision_id=int(existing["latest_revision_id"])
        )
        action = "Updated"
    run.save(episode_id=int(episode["id"]), audio_attached=True)
    typer.echo(f"{action} draft episode {episode['id']} ({plan.slug}).")
    if previous_audio_id not in {None, audio_id}:
        typer.echo(f"The previous Audio {previous_audio_id} is no longer used by the draft; delete it in Wagtail.")
    episode, contributors_ready = _ensure_contributors(run, episode)

    _start_transcript(run, episode, forced=transcript, ready=diarization_ready and contributors_ready)
    typer.echo(f"Preview: {client.absolute_url(episode.get('preview_url'))}")
    typer.echo(f"Edit:    {client.absolute_url(episode.get('edit_url'))}")
    typer.echo("Nothing was published. Publish with `podcast cms-publish` after checking the preview.")
    _report_steps(run.steps)


def _resolve_parent(client: DjangoCastClient, config: CmsConfig) -> int:
    parents = client.list_parents()
    podcasts = [parent for parent in parents if parent.get("type") == _PODCAST_TYPE]
    if config.parent_id is not None:
        if not any(parent.get("id") == config.parent_id for parent in podcasts):
            raise CmsApiError(
                f"cms.parent_id {config.parent_id} is not a podcast this token can add episodes to "
                f"({_describe_parents(podcasts)})."
            )
        return config.parent_id
    if config.parent_title is not None:
        podcasts = [parent for parent in podcasts if parent.get("title") == config.parent_title]
    if len(podcasts) != 1:
        raise CmsApiError(
            "Set cms.parent_id (or cms.parent_title) to the podcast page for new episodes; "
            f"GET {PARENTS_PATH} offers {_describe_parents(podcasts)}."
        )
    return int(podcasts[0]["id"])


def _describe_parents(parents: list[dict[str, Any]]) -> str:
    if not parents:
        return "no matching podcast"
    return ", ".join(f"{parent.get('id')}: {parent.get('title')}" for parent in parents)


def _find_existing_episode(
    client: DjangoCastClient, state: CmsDraftState, *, parent_id: int, slug: str
) -> dict[str, Any] | None:
    """The draft this workspace created (by stored id, which survives slug changes), else one with ``slug``."""
    if state.episode_id is not None:
        episode = client.get_episode(state.episode_id)
        if episode is not None and _parent_id(episode) == parent_id:
            return episode
        typer.echo(
            f"Episode {state.episode_id} from cms/draft.json is not under podcast {parent_id}; "
            "looking the draft up by slug."
        )
    post = client.find_post(parent_id=parent_id, slug=slug)
    if post is None:
        return None
    if post.get("type") != _EPISODE_TYPE:
        raise CmsApiError(f"Slug {slug!r} is already used by a {post.get('type')} page ({post.get('edit_url')}).")
    episode = client.get_episode(int(post["id"]))
    if episode is None:
        raise CmsApiError(f"Episode {post['id']} with slug {slug!r} disappeared while it was read; re-run.")
    return episode


def _parent_id(episode: dict[str, Any]) -> int | None:
    parent = episode.get("parent")
    return parent.get("id") if isinstance(parent, dict) else None


def _refuse_live(episode: dict[str, Any]) -> None:
    if episode.get("live"):
        raise CmsApiError(
            f"Episode {episode.get('id')} ({episode.get('slug')}) is already published; cms-draft only changes "
            f"unpublished drafts. Edit it in Wagtail: {episode.get('edit_url')}"
        )


def _audio_for_draft(
    run: _DraftRun, existing: dict[str, Any] | None, *, new_audio: bool
) -> tuple[int, dict[str, Any] | None]:
    """Return the Audio id for the draft and what the API reports about it (``None`` when it cannot be read)."""
    state = run.state
    unattached = state.audio_id if state.audio_id is not None and not state.audio_attached else None
    if not new_audio:
        # An Audio uploaded by a run whose episode request then failed wins over the one the draft still has.
        audio_id = unattached or _podcast_audio_id(existing) or state.audio_id
        if audio_id is not None:
            typer.echo(
                f"Reusing Audio {audio_id}. The editor API cannot change an Audio's title, subtitle, chapter marks "
                "or file, so they stay as uploaded; use --new-audio to upload the file again as a new Audio."
            )
            audio = run.client.find_audio(audio_id)
            if audio is None:
                typer.echo(
                    f"Audio {audio_id} is not in the editor audio list; its files and diarization mode are unknown."
                )
            same = state.audio_id == audio_id
            run.save(
                audio_id=audio_id,
                audio_edit_url=_audio_edit_url(run, audio) or (state.audio_edit_url if same else None),
                audio_attached=state.audio_attached if same else True,
                transcript_pending=state.transcript_pending if same else False,
            )
            return audio_id, audio

    if unattached is not None:
        typer.echo(f"Audio {unattached} was uploaded earlier but never attached to the draft; delete it in Wagtail.")
    upload = run.plan.audio
    if upload.diarization_mode == "enabled" and not run.client.supports_upload_diarization_mode(
        "enabled", collection=upload.collection
    ):
        # Uploading with "enabled" fails today; upload with the site default and set the mode afterwards.
        upload = replace(upload, diarization_mode=None)
    typer.echo(f"Uploading {audio_file_label(upload.file)} to {AUDIOS_PATH} ...")
    audio = run.client.upload_audio(upload)
    audio_id = int(audio["id"])
    # Remember the Audio and its pending work before the episode request: if that fails, a re-run attaches this
    # Audio instead of uploading again, and still adds its formats and starts (or lists) its transcript.
    run.save(
        audio_id=audio_id,
        audio_edit_url=_audio_edit_url(run, audio),
        audio_attached=False,
        transcript_pending=True,
    )
    typer.echo(f"Uploaded Audio {audio_id}.")
    return audio_id, audio


def _audio_edit_url(run: _DraftRun, audio: dict[str, Any] | None) -> str | None:
    return run.client.absolute_url(audio.get("edit_url")) if audio is not None else None


def _podcast_audio_id(episode: dict[str, Any] | None) -> int | None:
    if episode is None:
        return None
    audio = episode.get("podcast_audio")
    if isinstance(audio, dict) and isinstance(audio.get("id"), int):
        return int(audio["id"])
    return None


def _add_missing_formats(run: _DraftRun, audio_id: int, audio: dict[str, Any] | None) -> None:
    """Add the further formats the Audio confirmably lacks; list a check when its files cannot be read."""
    extra = run.plan.extra_audio_files
    if not extra:
        return
    if audio is None:
        # Uploading blindly could replace files that are already there.
        files = "; ".join(f"{audio_file.field.upper()}: {audio_file.path}" for audio_file in extra)
        run.steps.append(
            f"Check that {run.audio_editor(audio_id)} has the further audio formats and add the missing ones, one "
            f"file per field (the editor API does not report the files of Audio {audio_id}): {files}"
        )
        return
    missing = tuple(item for item in extra if not audio.get(item.field))
    if not missing:
        return
    if run.audio_patch_supported(audio_id):
        for audio_file in missing:
            typer.echo(f"Adding {audio_file_label(audio_file)} to Audio {audio_id} ...")
            run.client.add_audio_file(audio_id, audio_file)
        return
    files = "; ".join(f"{audio_file.field.upper()}: {audio_file.path}" for audio_file in missing)
    run.steps.append(
        f"Add the further audio formats in {run.audio_editor(audio_id)}, one file per field, and save "
        f"(the editor API cannot add files to an Audio yet): {files}"
    )


def _ensure_diarization(run: _DraftRun, audio_id: int, audio: dict[str, Any] | None) -> bool:
    """Bring the Audio to the configured diarization mode; return whether it is confirmed ``enabled``."""
    wanted = run.plan.audio.diarization_mode or "enabled"
    reported = audio.get("transcript_diarization_mode") if audio is not None else None
    current = reported if isinstance(reported, str) else None
    label = _DIARIZATION_LABELS.get(wanted, wanted)
    if current is None:
        run.steps.append(
            f'Check that "Transcript diarization mode" is {label} in {run.audio_editor(audio_id)} (the editor API '
            f"does not report the mode of Audio {audio_id})."
        )
        return False
    if current != wanted:
        if not run.audio_patch_supported(audio_id):
            run.steps.append(
                f'Set "Transcript diarization mode" to {label} in {run.audio_editor(audio_id)} and save (the '
                f"upload API does not accept {wanted!r} yet; the Audio has {current!r}, and with Inherit the "
                "site's Voxhelm settings decide, which can mean no speaker labels)."
            )
            return False
        stored = run.client.set_audio_diarization_mode(audio_id, wanted).get("transcript_diarization_mode")
        if stored != wanted:
            run.steps.append(
                f'Set "Transcript diarization mode" to {label} in {run.audio_editor(audio_id)} and save (the editor '
                f"API did not store it; Audio {audio_id} reports {stored!r})."
            )
            return False
        current = wanted
        typer.echo(f"Set the transcript diarization mode of Audio {audio_id} to {wanted}.")
    return current == "enabled"


def _ensure_contributors(run: _DraftRun, episode: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Credit the episode's hosts as contributors; return the episode and whether that is confirmed complete."""
    plan = run.plan
    edit_url = run.client.absolute_url(episode.get("edit_url"))
    if not plan.hosts_listed:
        run.steps.append(
            f"Add the episode's speakers in the Contributors section of {edit_url} and save the draft (episode.yaml "
            "lists no hosts; list them under hosts and map them in cms.contributors to automate this)."
        )
        return episode, False
    complete = True
    if plan.unmapped_hosts:
        run.steps.append(
            f"Credit {', '.join(plan.unmapped_hosts)} in the Contributors section of {edit_url} (no "
            "cms.contributors entry maps them to a django-cast contributor; add one to automate this)."
        )
        complete = False
    wanted = plan.contributors_payload()
    if not wanted:
        return episode, complete
    if "contributors" not in episode:
        run.steps.append(
            f"Add the contributors in the Contributors section of {edit_url} and save the draft (the editor API "
            "cannot set contributors yet; the transcript uses them for the speakers): "
            f"{_describe_contributors(wanted)}"
        )
        return episode, False
    if _contributor_refs(episode["contributors"]) != wanted:
        episode = run.client.update_episode(
            int(episode["id"]),
            {"contributors": wanted},
            base_revision_id=int(episode["latest_revision_id"]),
        )
        stored = _contributor_refs(episode.get("contributors"))
        if stored != wanted:
            run.steps.append(
                f"Add the contributors in the Contributors section of {edit_url} and save the draft (the editor API "
                f"did not store them; it returned {_describe_contributors(stored) or 'none'}): "
                f"{_describe_contributors(wanted)}"
            )
            return episode, False
        typer.echo(f"Set contributors: {_describe_contributors(wanted)}.")
    return episode, complete


def _contributor_refs(value: object) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        contributor = item.get("contributor")
        slug = contributor.get("slug") if isinstance(contributor, dict) else item.get("slug")
        refs.append({"slug": str(slug), "role": str(item.get("role"))})
    return refs


def _describe_contributors(refs: list[dict[str, str]]) -> str:
    return ", ".join(f"{ref['slug']} ({ref['role']})" for ref in refs)


def _start_transcript(run: _DraftRun, episode: dict[str, Any], *, forced: bool, ready: bool) -> None:
    """Start the transcript of a new Audio (or on --transcript) once speaker labels are ensured."""
    if not (forced or run.state.transcript_pending):
        typer.echo("Transcript: not requested (the Audio was reused); pass --transcript to start it.")
        return
    episode_id = int(episode["id"])
    edit_url = run.client.absolute_url(episode.get("edit_url"))
    supported = run.client.supports_transcript_generation(episode_id)
    if ready and supported:
        run.client.start_transcript_generation(episode_id)
        run.save(transcript_pending=False)
        typer.echo("Transcript generation enqueued (Voxhelm).")
        return
    if ready:
        run.steps.append(
            f'Click "Generate transcript" in {edit_url} (the editor API cannot start transcript generation yet).'
        )
        run.save(transcript_pending=False)
        return
    reason = (
        "not started automatically: speaker labels need diarization mode Enabled on the Audio and the episode's "
        "contributors"
    )
    if supported:
        # Keep it pending: a re-run starts it once the steps above are done.
        run.steps.append(f'Then re-run `podcast cms-draft`, or click "Generate transcript" in {edit_url} ({reason}).')
        run.save(transcript_pending=True)
        return
    run.steps.append(f'Then click "Generate transcript" in {edit_url} ({reason}).')
    run.save(transcript_pending=False)


def _report_steps(steps: list[str]) -> None:
    if not steps:
        typer.echo("No manual steps remain.")
        return
    typer.echo("Remaining manual steps in Wagtail, in this order:")
    for number, step in enumerate(steps, start=1):
        typer.echo(f"  {number}. {step}")


def _publish(
    client: DjangoCastClient,
    store: EpisodeWorkspaceStore,
    config: CmsConfig,
    *,
    slug: str,
    yes: bool,
) -> None:
    state = _read_state(store, config)
    parent_id = _resolve_parent(client, config)
    episode = _find_existing_episode(client, state, parent_id=parent_id, slug=slug)
    if episode is None:
        raise CmsApiError(f"No draft episode {slug!r} found; run `podcast cms-draft` first.")
    if episode.get("status") == "live":
        raise CmsApiError(f"Episode {episode['id']} ({episode.get('slug')}) is already live without a newer draft.")
    if _podcast_audio_id(episode) is None:
        raise CmsApiError(f"Episode {episode['id']} has no podcast audio; run `podcast cms-draft` first.")
    typer.echo(f"Episode {episode['id']}: {episode.get('title')}")
    typer.echo(f"  slug: {episode.get('slug')}, status: {episode.get('status')}, audio: {_podcast_audio_id(episode)}")
    typer.echo(f"  preview: {client.absolute_url(episode.get('preview_url'))}")
    if not yes and not typer.confirm("Publish this episode on the live site now?", default=False):
        typer.echo("Not published.")
        raise typer.Exit(code=1)
    published = client.publish_episode(int(episode["id"]), revision_id=int(episode["latest_revision_id"]))
    typer.echo(f"Published episode {published.get('id')}: {client.absolute_url(published.get('public_url'))}")


def _read_state(store: EpisodeWorkspaceStore, config: CmsConfig) -> CmsDraftState:
    """The stored ids for ``config.base_url``; ids stored for another site are ignored."""
    path = store.layout.cms_draft_json
    empty = CmsDraftState(base_url=config.base_url)
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CmsApiError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("base_url") != config.base_url:
        return empty
    return CmsDraftState(
        base_url=config.base_url,
        episode_id=_optional_id(data.get("episode_id")),
        audio_id=_optional_id(data.get("audio_id")),
        audio_edit_url=_optional_text(data.get("audio_edit_url")),
        audio_attached=data.get("audio_attached") is not False,
        transcript_pending=data.get("transcript_pending") is True,
    )


def _write_state(store: EpisodeWorkspaceStore, state: CmsDraftState) -> None:
    path = store.layout.cms_draft_json
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "base_url": state.base_url,
        "episode_id": state.episode_id,
        "audio_id": state.audio_id,
        "audio_edit_url": state.audio_edit_url,
        "audio_attached": state.audio_attached,
        "transcript_pending": state.transcript_pending,
    }
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")


def _optional_id(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


class CmsDraftInProgressError(CmsApiError):
    """Another ``cms-draft`` or ``cms-publish`` run holds the workspace's CMS lock."""


@contextmanager
def _cms_lock(store: EpisodeWorkspaceStore) -> Iterator[None]:
    lock_path = store.layout.cms_dir / ".cms.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CmsDraftInProgressError(f"Another CMS draft run is already active for {store.layout.root}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
