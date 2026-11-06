# Collaborate on notebooks

Open a notebook and choose **Collaborate**. Save any pending changes in the
regular editor first. Project members can inspect the current discussion and
presence; editors and owners can post comments, request reviews and edit cells.

## Live editing

1. Choose **Enable live editing**. The regular editor becomes read-only.
2. Choose **Edit cell** on an available cell. That cell has one editing owner;
   teammates can edit other cells simultaneously.
3. Text saves automatically after a short pause. Check the **Saved** indicator,
   then choose **Finish editing** to release the cell.
4. Finish all active cell edits and choose **Disable live editing** before
   adding, deleting or reordering cells, importing Git source, restoring a source
   revision, or moving/deleting the notebook.

Cells have short-lived leases, renewed while their page remains active. An
expired lease or stale source stops saving and retains the local draft. Choose
**Reconnect editing** when the saved cell is unchanged; otherwise copy the draft
before closing and compare it with the current source. Management restart clears
presence and leases, while saved source remains durable. A lease is an editing
coordination mechanism, not an execution or operating-system isolation boundary.

## Discuss and review

Post a comment, reply to a root thread, and resolve/reopen the thread when its
question is answered. Only the author may replace their comment text. Discussion
and review lists are paginated. Moving a notebook to another project does not
expose the previous project's discussion to its new members.

Choose a different editor/owner as reviewer. A request captures the current
source hash; its assigned reviewer may approve or request changes. Subsequent
source changes mark the review stale and require a new request. Reviews are
advisory and do not silently change run or deployment permissions.

Project owners and workspace administrators can inspect the **Audit history**
screen, filter exact actions/actors, and export up to 10,000 matching events as
CSV. Administrators can also inspect historical actions from deleted projects.
Audit history follows configured record retention.
