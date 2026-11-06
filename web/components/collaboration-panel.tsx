"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  Check,
  LockKeyhole,
  MessageSquare,
  Save,
  Users,
  X,
} from "lucide-react";
import {
  api,
  date,
  json,
  type Notebook,
  type Page,
  type User,
} from "@/lib/api";
import { Field, Modal, Pagination, Spinner, Status } from "./ui";
import "./v5-collaboration.css";

const CodeEditor = dynamic(() => import("./code-editor"), { ssr: false });
type Participant = {
  user_id: string;
  username: string;
  client_id: string;
  cell_id?: string;
};
type Room = {
  enabled: boolean;
  version: number;
  notebook: Notebook | null;
  presence: Participant[];
  leases: Participant[];
};
type Comment = {
  id: string;
  text: string;
  author_id: string;
  author_name: string;
  parent_id?: string;
  cell_id?: string;
  resolved: boolean;
  created_at: string;
};
type Review = {
  id: string;
  reviewer_id: string;
  requester_name: string;
  reviewer_name: string;
  status: string;
  message: string;
  decision_message?: string;
  stale: boolean;
  created_at: string;
};
type Draft = {
  cell_id: string;
  token: string;
  base_hash: string;
  source: string;
  savedSource: string;
};

export default function CollaborationPanel({
  notebookId,
  canEdit,
  onClose,
  onChanged,
}: {
  notebookId: string;
  canEdit: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [book, setBook] = useState<Notebook | null>(null);
  const [room, setRoom] = useState<Room | null>(null);
  const [user, setUser] = useState<User | null>(null);
  const [comments, setComments] = useState<Page<Comment> | null>(null);
  const [reviews, setReviews] = useState<Page<Review> | null>(null);
  const [reviewsVersion, setReviewsVersion] = useState<number | undefined>(
    undefined,
  );
  const [offset, setOffset] = useState(0);
  const [reviewOffset, setReviewOffset] = useState(0);
  const [tab, setTab] = useState<"discussion" | "reviews">("discussion");
  const [message, setMessage] = useState("");
  const [reply, setReply] = useState<Comment | null>(null);
  const [reviewerId, setReviewerId] = useState("");
  const [members, setMembers] = useState<
    { user_id: string; username?: string; role: string }[]
  >([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  const draftRef = useRef<Draft | null>(null);
  const version = useRef<number | undefined>(undefined);
  const [clientId] = useState(() => crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  const savingRef = useRef(false);
  const project = book?.project_id || "default";

  const poll = useCallback(async () => {
    const value = await api<Room>(
      `/notebooks/${notebookId}/collaboration${version.current ? `?known_version=${version.current}` : ""}`,
    );
    if (!alive.current || (version.current && value.version < version.current))
      return;
    setRoom(value);
    if (value.notebook) setBook(value.notebook);
    version.current = value.version;
  }, [notebookId]);
  const loadDiscussion = useCallback(async () => {
    const requestedVersion = version.current;
    const [discussion, requests] = await Promise.all([
      api<Page<Comment>>(
        `/notebooks/${notebookId}/comments?limit=20&offset=${offset}`,
      ),
      api<Page<Review>>(
        `/notebooks/${notebookId}/reviews?limit=10&offset=${reviewOffset}`,
      ),
    ]);
    if (alive.current && requestedVersion === version.current) {
      setComments(discussion);
      setReviews(requests);
      setReviewsVersion(requestedVersion);
    }
  }, [notebookId, offset, reviewOffset]);

  useEffect(() => {
    alive.current = true;
    void Promise.all([
      poll(),
      api<User>("/auth/me").then((value) => {
        if (alive.current) setUser(value);
      }),
    ]).catch((value) => {
      if (alive.current) setError(value.message);
    });
    let polling = false;
    const timer = setInterval(() => {
      if (polling || document.hidden) return;
      polling = true;
      void poll()
        .catch((value) => {
          if (alive.current) setError(value.message);
        })
        .finally(() => {
          polling = false;
        });
    }, 1500);
    const heartbeat = () =>
      api(
        `/notebooks/${notebookId}/presence`,
        json("POST", {
          client_id: clientId,
          cell_id: draftRef.current?.cell_id || null,
        }),
      );
    void heartbeat().catch(() => {});
    const presenceTimer = setInterval(() => {
      if (!document.hidden) void heartbeat().catch(() => {});
    }, 10000);
    return () => {
      alive.current = false;
      clearInterval(timer);
      clearInterval(presenceTimer);
    };
  }, [clientId, notebookId, poll]);

  useEffect(() => {
    void loadDiscussion().catch((value) => {
      if (alive.current) setError(value.message);
    });
  }, [loadDiscussion, room?.version]);
  useEffect(() => {
    let pending = false;
    const timer = setInterval(() => {
      if (pending || document.hidden) return;
      pending = true;
      void loadDiscussion()
        .catch((value) => {
          if (alive.current) setError(value.message);
        })
        .finally(() => {
          pending = false;
        });
    }, 5000);
    return () => clearInterval(timer);
  }, [loadDiscussion]);
  useEffect(() => {
    if (!canEdit || !book) return;
    let stopped = false;
    api<{ user_id: string; username?: string; role: string }[]>(
      `/projects/${project}/members`,
    )
      .then((value) => {
        if (!stopped) setMembers(value);
      })
      .catch(() => {
        /* Editors may enter an assigned reviewer's account ID if member administration is unavailable. */
      });
    return () => {
      stopped = true;
    };
  }, [canEdit, !!book, project]);
  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (
        draftRef.current &&
        draftRef.current.source !== draftRef.current.savedSource
      )
        event.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);

  function updateDraft(value: Draft | null) {
    draftRef.current = value;
    setDraft(value);
  }
  async function saveDraft() {
    const current = draftRef.current;
    if (!current || savingRef.current || current.source === current.savedSource)
      return;
    savingRef.current = true;
    setSaving(true);
    const sent = current.source;
    try {
      const result = await api<{ version: number; source_hash: string }>(
        `/notebooks/${notebookId}/collaboration/cell`,
        json("PATCH", {
          client_id: clientId,
          cell_id: current.cell_id,
          token: current.token,
          base_hash: current.base_hash,
          source: sent,
        }),
      );
      const latest = draftRef.current;
      if (latest && latest.cell_id === current.cell_id)
        updateDraft({
          ...latest,
          base_hash: result.source_hash,
          savedSource: sent,
        });
      setError("");
      await poll();
    } catch (value) {
      setError((value as Error).message);
      throw value;
    } finally {
      savingRef.current = false;
      if (alive.current) setSaving(false);
    }
  }
  useEffect(() => {
    if (!draft || draft.source === draft.savedSource || error) return;
    const timer = setTimeout(() => {
      void saveDraft().catch(() => {});
    }, 700);
    return () => clearTimeout(timer);
  }, [draft?.source, draft?.savedSource, saving, error]);

  async function act(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await task();
    } catch (value) {
      setError((value as Error).message);
    } finally {
      if (alive.current) setBusy(false);
    }
  }
  async function finishEdit() {
    if (savingRef.current)
      throw new Error("Wait for the current cell save to finish");
    await saveDraft();
    const current = draftRef.current;
    if (current && current.source !== current.savedSource)
      throw new Error(
        "Text changed during the save. Your draft is retained; finish saving it before closing",
      );
    if (current)
      await api(
        `/notebooks/${notebookId}/collaboration/release`,
        json("POST", {
          client_id: clientId,
          ...current,
          base_hash: current.base_hash,
        }),
      );
    updateDraft(null);
    await poll();
    onChanged();
  }
  function close() {
    void act(async () => {
      await finishEdit();
      onClose();
    });
  }
  async function reconnect() {
    const current = draftRef.current;
    if (!current) return;
    const lease = await api<{ token: string; source_hash: string }>(
      `/notebooks/${notebookId}/collaboration/lease`,
      json("POST", { client_id: clientId, cell_id: current.cell_id }),
    );
    const latest = await api<Notebook>(`/notebooks/${notebookId}`);
    const cell = latest.cells.find((value) => value.id === current.cell_id);
    if (!cell || cell.source !== current.savedSource)
      throw new Error(
        "The saved cell changed while you were away. Copy your local draft before closing and reloading the notebook",
      );
    updateDraft({
      ...current,
      token: lease.token,
      base_hash: lease.source_hash,
    });
    setError("");
  }

  return (
    <Modal title="Collaborate on notebook" onClose={close} wide>
      <div className="v5-collaboration">
        {error ? (
          <div className="inline-error" role="alert">
            {error}
            <span>
              {" "}
              Your draft is retained below. Copy it before leaving if the
              editing lease expired.
            </span>
            {draft ? (
              <>
                <button
                  className="button"
                  disabled={busy}
                  onClick={() => void act(reconnect)}
                >
                  Reconnect editing
                </button>
                <button
                  className="button"
                  onClick={() => {
                    if (
                      window.confirm(
                        "Discard the local draft and close? Previously saved cell changes remain saved.",
                      )
                    ) {
                      updateDraft(null);
                      onChanged();
                      onClose();
                    }
                  }}
                >
                  Discard local draft and close
                </button>
              </>
            ) : (
              <button className="button" onClick={() => setError("")}>
                Dismiss
              </button>
            )}
          </div>
        ) : null}
        {!book || !room ? (
          <Spinner />
        ) : (
          <>
            <div className="v5-room-heading">
              <div>
                <h3>{book.name}</h3>
                <p className="muted">
                  <Users size={14} />
                  {room.presence.length} open session
                  {room.presence.length === 1 ? "" : "s"} ·{" "}
                  {room.presence.map((value) => value.username).join(", ") ||
                    "Connecting"}
                </p>
              </div>
              {canEdit ? (
                <button
                  className="button"
                  disabled={busy || !!draft}
                  onClick={() =>
                    void act(async () => {
                      await api(
                        `/notebooks/${notebookId}/collaboration`,
                        json("PUT", {
                          enabled: !room.enabled,
                          version: room.version,
                        }),
                      );
                      await poll();
                      onChanged();
                    })
                  }
                >
                  {room.enabled
                    ? "Disable live editing"
                    : "Enable live editing"}
                </button>
              ) : null}
            </div>
            <p className="form-note">
              Live editing saves each cell as you type. Choose a cell to edit;
              teammates can edit other cells at the same time. Finish all edits
              and disable live editing to add, remove, reorder, or import cells.
            </p>
            <div className="v5-collaboration-columns">
              <section aria-label="Live notebook cells">
                {book.cells.map((cell, index) => {
                  const owner = room.leases.find(
                    (value) => value.cell_id === cell.id,
                  );
                  const editing = draft?.cell_id === cell.id;
                  return (
                    <article className="v5-live-cell" key={cell.id}>
                      <div className="v5-cell-heading">
                        <strong>
                          Cell {index + 1} · {cell.language}
                        </strong>
                        {owner ? (
                          <small>
                            <LockKeyhole size={12} /> {owner.username}
                          </small>
                        ) : null}
                        {editing ? (
                          <button
                            className="button"
                            disabled={busy || saving}
                            onClick={() => void act(finishEdit)}
                          >
                            <Check size={13} />
                            Finish editing
                          </button>
                        ) : canEdit && room.enabled ? (
                          <button
                            className="button"
                            disabled={busy || !!draft || !!owner}
                            onClick={() =>
                              void act(async () => {
                                const result = await api<{
                                  token: string;
                                  source_hash: string;
                                }>(
                                  `/notebooks/${notebookId}/collaboration/lease`,
                                  json("POST", {
                                    client_id: clientId,
                                    cell_id: cell.id,
                                  }),
                                );
                                const current = await api<Notebook>(
                                  `/notebooks/${notebookId}`,
                                );
                                const fresh = current.cells.find(
                                  (value) => value.id === cell.id,
                                );
                                if (!fresh)
                                  throw new Error(
                                    "Cell was removed. Reload the notebook",
                                  );
                                updateDraft({
                                  cell_id: cell.id,
                                  token: result.token,
                                  base_hash: result.source_hash,
                                  source: fresh.source,
                                  savedSource: fresh.source,
                                });
                                await poll();
                              })
                            }
                          >
                            Edit cell
                          </button>
                        ) : null}
                      </div>
                      {editing ? (
                        <>
                          <CodeEditor
                            disabled={busy}
                            label={`Live cell ${index + 1}`}
                            language={cell.language}
                            value={draft.source}
                            onChange={(source) =>
                              updateDraft({ ...draftRef.current!, source })
                            }
                          />
                          <div className="v5-save-state" aria-live="polite">
                            {saving ? (
                              <>
                                <Spinner />
                                Saving…
                              </>
                            ) : draft.source === draft.savedSource ? (
                              <>
                                <Check size={12} />
                                Saved
                              </>
                            ) : (
                              <>
                                <Save size={12} />
                                Unsaved draft
                              </>
                            )}
                          </div>
                        </>
                      ) : (
                        <pre>{cell.source || "Empty cell"}</pre>
                      )}
                    </article>
                  );
                })}
              </section>
              <aside aria-label="Notebook discussion and reviews">
                <div className="segmented">
                  <button
                    className={tab === "discussion" ? "active" : ""}
                    onClick={() => setTab("discussion")}
                  >
                    <MessageSquare size={14} />
                    Discussion
                  </button>
                  <button
                    className={tab === "reviews" ? "active" : ""}
                    onClick={() => setTab("reviews")}
                  >
                    Reviews
                  </button>
                </div>
                {tab === "discussion" ? (
                  <>
                    {canEdit ? (
                      <form
                        onSubmit={(event) => {
                          event.preventDefault();
                          void act(async () => {
                            await api(
                              `/notebooks/${notebookId}/comments`,
                              json("POST", {
                                text: message,
                                parent_id:
                                  reply?.parent_id || reply?.id || null,
                              }),
                            );
                            setMessage("");
                            setReply(null);
                            await loadDiscussion();
                          });
                        }}
                      >
                        {reply ? (
                          <div className="v5-reply-note">
                            Replying to {reply.author_name}
                            <button
                              type="button"
                              className="icon-button"
                              aria-label="Cancel reply"
                              onClick={() => setReply(null)}
                            >
                              <X size={12} />
                            </button>
                          </div>
                        ) : null}
                        <Field label="Comment">
                          <textarea
                            required
                            maxLength={4000}
                            rows={3}
                            value={message}
                            onChange={(event) => setMessage(event.target.value)}
                          />
                        </Field>
                        <button
                          className="button primary"
                          disabled={busy || !message.trim()}
                        >
                          Post comment
                        </button>
                      </form>
                    ) : (
                      <p className="muted">
                        Project editors can post comments.
                      </p>
                    )}
                    {comments?.items.length === 0 ? (
                      <p className="muted">No discussion yet.</p>
                    ) : null}
                    {[...(comments?.items || [])].reverse().map((item) => (
                      <article
                        className={`v5-comment ${item.parent_id ? "reply" : ""}`}
                        key={item.id}
                      >
                        <div>
                          <strong>{item.author_name}</strong>
                          <small>{date(item.created_at)}</small>
                        </div>
                        {item.parent_id ? (
                          <small className="muted">
                            Reply to thread {item.parent_id.slice(0, 8)}
                          </small>
                        ) : null}
                        <p>{item.text}</p>
                        {item.resolved ? <Status status="resolved" /> : null}
                        {canEdit ? (
                          <div className="v5-inline-actions">
                            <button
                              className="button"
                              onClick={() => setReply(item)}
                            >
                              Reply
                            </button>
                            {!item.parent_id ? (
                              <button
                                className="button"
                                disabled={busy}
                                onClick={() =>
                                  void act(async () => {
                                    await api(
                                      `/notebooks/${notebookId}/comments/${item.id}`,
                                      json("PATCH", {
                                        resolved: !item.resolved,
                                      }),
                                    );
                                    await loadDiscussion();
                                  })
                                }
                              >
                                {item.resolved ? "Reopen" : "Resolve"}
                              </button>
                            ) : null}
                          </div>
                        ) : null}
                      </article>
                    ))}
                    <Pagination
                      total={comments?.total || 0}
                      limit={20}
                      offset={offset}
                      onPage={setOffset}
                    />
                  </>
                ) : (
                  <>
                    <p className="form-note">
                      Reviews capture a source version. A later source edit
                      makes an approval stale. Reviews are advisory.
                    </p>
                    {canEdit ? (
                      <form
                        onSubmit={(event) => {
                          event.preventDefault();
                          void act(async () => {
                            await api(
                              `/notebooks/${notebookId}/reviews`,
                              json("POST", {
                                reviewer_id: reviewerId,
                                message,
                              }),
                            );
                            setMessage("");
                            await loadDiscussion();
                          });
                        }}
                      >
                        <Field
                          label="Reviewer account ID"
                          hint="Choose another project editor or owner."
                        >
                          {members.length ? (
                            <select
                              required
                              value={reviewerId}
                              onChange={(event) =>
                                setReviewerId(event.target.value)
                              }
                            >
                              <option value="">Select reviewer</option>
                              {members
                                .filter(
                                  (value) =>
                                    value.user_id !== user?.id &&
                                    ["editor", "owner"].includes(value.role),
                                )
                                .map((value) => (
                                  <option
                                    key={value.user_id}
                                    value={value.user_id}
                                  >
                                    {value.username || value.user_id}
                                  </option>
                                ))}
                            </select>
                          ) : (
                            <input
                              required
                              value={reviewerId}
                              onChange={(event) =>
                                setReviewerId(event.target.value)
                              }
                            />
                          )}
                        </Field>
                        <Field label="Review message">
                          <textarea
                            maxLength={4000}
                            rows={2}
                            value={message}
                            onChange={(event) => setMessage(event.target.value)}
                          />
                        </Field>
                        <button className="button primary" disabled={busy}>
                          Request review
                        </button>
                      </form>
                    ) : null}
                    {reviews?.items.length === 0 ? (
                      <p className="muted">No review requests.</p>
                    ) : null}
                    {reviews?.items.map((item) => (
                      <article className="v5-comment" key={item.id}>
                        <strong>
                          {item.requester_name} → {item.reviewer_name}
                        </strong>
                        <Status
                          status={
                            reviewsVersion !== room.version
                              ? "checking"
                              : item.stale
                                ? "stale"
                                : item.status
                          }
                        />
                        <p>{item.message}</p>
                        {item.decision_message ? (
                          <p>{item.decision_message}</p>
                        ) : null}
                        {item.status === "open" &&
                        !item.stale &&
                        reviewsVersion === room.version &&
                        item.reviewer_id === user?.id ? (
                          <div className="v5-inline-actions">
                            {(["approved", "changes_requested"] as const).map(
                              (decision) => (
                                <button
                                  className="button"
                                  disabled={busy}
                                  key={decision}
                                  onClick={() =>
                                    void act(async () => {
                                      await api(
                                        `/notebooks/${notebookId}/reviews/${item.id}/decision`,
                                        json("POST", { decision }),
                                      );
                                      await loadDiscussion();
                                    })
                                  }
                                >
                                  {decision === "approved"
                                    ? "Approve"
                                    : "Request changes"}
                                </button>
                              ),
                            )}
                          </div>
                        ) : null}
                      </article>
                    ))}
                    <Pagination
                      total={reviews?.total || 0}
                      limit={10}
                      offset={reviewOffset}
                      onPage={setReviewOffset}
                    />
                  </>
                )}
              </aside>
            </div>
          </>
        )}
      </div>
    </Modal>
  );
}
