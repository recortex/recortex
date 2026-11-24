"""Read the shared hub ledger, freeze local previews and verify each append."""
import argparse
import hashlib
import hmac
import json
import os
import re
import sys

import index
import observer_store
import project_map as model
import record_store
import recortex
import storage

MapError = model.MapError
TOO_LARGE_SEED = "This first map is too large for one hub comment. Seed fewer branches, then add the rest."


def scope(repo, hub):
    config = recortex.load_config()
    if (not recortex.valid_repo(repo) or not isinstance(config.get("repo"), str)
            or config["repo"].lower() != repo.lower() or type(hub) is not int
            or hub < 1 or type(config.get("hub")) is not int or config["hub"] != hub):
        raise MapError("The configured project or hub has changed.")
    allowed = recortex.allowed_users(repo)
    if not allowed:
        raise MapError("Project Map requires a trusted project.")
    return allowed


def account(repo, hub):
    allowed = scope(repo, hub)
    status, name = record_store.for_repo(repo).account_state()
    if status != "ready" or not isinstance(name, str):
        raise MapError("Sign in with an allowed project account.")
    if name.lower() not in allowed:
        raise MapError("The signed-in account is outside the project's trusted list. For an organization-owned "
                       "repository, add your GitHub login beside the repository name in its trusted-repos entry.")
    return name.lower()


def verified_rows(repo, hub, marker):
    """Fetch trusted, immutable comments for one project ledger, including edit-history checks."""
    allowed = scope(repo, hub)
    records = record_store.for_repo(repo)
    expected = records.api_issue(hub).lower()
    raw = records.list_entries(hub)
    if not raw.strip():
        raise MapError("The hub comment listing was incomplete.")
    rows = {}
    for row in recortex.parse_json_stream(raw):
        if (not isinstance(row, dict) or str(row.get("issue_url", "")).lower() != expected or
                type(row.get("id")) is not int or row["id"] <= 0):
            raise MapError("The hub comment listing was incomplete.")
        body = row.get("body") or ""
        author = (row.get("user") or {}).get("login") or ""
        if (not body.startswith(marker) or author.lower() not in allowed or not row.get("created_at")
                or row["created_at"] != row.get("updated_at")):
            continue
        node = row.get("node_id")
        if not isinstance(node, str) or not index.COMMENT_NODE.fullmatch(node):
            raise MapError("The hub comment edit history could not be verified.")
        rows[row["id"]] = dict(row, author=author)
    ids = sorted(rows)
    verified = []
    for start in range(0, len(ids), index.GRAPHQL_BATCH):
        batch = ids[start:start + index.GRAPHQL_BATCH]
        answer = json.loads(records.histories({cid: (rows[cid]["node_id"], None) for cid in batch}))
        for cid in batch:
            node = (answer.get("data") or {}).get("c%d" % cid) or {}
            edits = node.get("userContentEdits") or {}
            if (answer.get("errors") or str(node.get("fullDatabaseId")) != str(cid) or
                    "lastEditedAt" not in node or not isinstance(edits.get("nodes"), list) or
                    type((edits.get("pageInfo") or {}).get("hasNextPage")) is not bool):
                raise MapError("The hub comment edit history could not be verified.")
            if node["lastEditedAt"] is None and edits["nodes"] == [] and not edits["pageInfo"]["hasNextPage"]:
                verified.append(rows[cid])
    return verified


def current(repo, hub):
    rows = verified_rows(repo, hub, model.MARKER)
    return model.replay(repo, hub, scope(repo, hub), rows)


def folder(repo, hub):
    root = os.path.realpath(recortex.project_root())
    return storage.folder(storage.state_top(), storage.repo_folder(repo.lower()), "project-map",
                          model.digest([root, hub])[:32])


def receipt_path(repo, hub, op_id):
    return os.path.join(folder(repo, hub), model.ident(op_id) + ".json")


def signature(data):
    return hmac.new(storage.user_key(), model.canonical(data).encode("utf-8"), hashlib.sha256).hexdigest()


def save(path, data):
    storage.write_file(path, model.canonical({"data": data, "signature": signature(data)}))
    for target, flags in ((path, getattr(os, "O_NOFOLLOW", 0)), (os.path.dirname(path), getattr(os, "O_DIRECTORY", 0))):
        handle = os.open(target, os.O_RDONLY | flags)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)


def load(repo, hub, op_id):
    raw = storage.read_file(receipt_path(repo, hub, op_id))
    if raw is None:
        return None
    signed = json.loads(raw)
    data = signed.get("data") or {}
    if (not hmac.compare_digest(signature(data), signed.get("signature", "")) or data.get("repo") != repo.lower()
            or data.get("hub") != hub or data.get("root") != os.path.realpath(recortex.project_root())
            or data.get("operation", {}).get("id") != op_id):
        raise MapError("This preview does not match the project.")
    return data


def check_content(repo, operation):
    """Apply the project's privacy rules to individual posted values before keeping or sending them."""
    import observer_publisher
    import observer_worker
    config = recortex.load_config()
    pending = list(operation["change"].items())
    # A move, split, or removal can put existing ancestor and child identifiers in the posted preconditions.
    pending.extend(("id", key) for key in operation["expected"])
    while pending:
        field, value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.items())
        elif isinstance(value, list):
            pending.extend((field, element) for element in value)
        elif isinstance(value, str):
            outbound = value
            if field == "evidence" and value.startswith("file:"):
                outbound = value[len("file:"):]
                if (outbound.startswith(("/", "~", "\\")) or "\\" in outbound
                        or any(part in ("", ".", "..") for part in outbound.split("/"))):
                    raise MapError("File evidence requires a relative project path.")
            try:
                private = observer_worker.privacy_problem(value, config) or (
                    outbound != value and observer_worker.privacy_problem(outbound, config))
            except (TypeError, AttributeError, re.error) as error:
                raise MapError("The project's privacy rules could not be checked.") from error
            if private or observer_publisher.outbound_problem(outbound, repo):
                raise MapError("A map value was refused by the project's publication checks.")


def remember(repo, hub, operation, channel="manual"):
    if channel not in ("manual", "observer") or (operation.get("source") == "observer" and channel != "observer"):
        raise MapError("Automatic publication requires observer authorization.")
    who = account(repo, hub)
    with observer_store._exclusive_lock(folder(repo, hub), "ledger.lock"):
        check_content(repo, operation)
        data = load(repo, hub, operation["id"])
        if data:
            if data["operation"] != operation or data["account"] != who or data.get("channel") != channel:
                raise MapError("This preview belongs to a different change or account.")
        else:
            save(receipt_path(repo, hub, operation["id"]),
                 {"repo": repo.lower(), "hub": hub, "root": os.path.realpath(recortex.project_root()),
                  "operation": operation, "account": who, "channel": channel, "sending": False})


def result(state, operation):
    event = next(e for e in state["history"] if e["id"] == operation["id"])
    return {"state": state, "operation": operation, "affected": event["affected"], "comment": event["comment"]}


def preview(repo, hub, change, source="user", op_id=None):
    state = current(repo, hub)
    prior = load(repo, hub, op_id) if op_id is not None else None
    if prior:
        operation = prior["operation"]
        normalized = dict(change)
        if normalized.get("action") == "add":
            normalized.setdefault("id", operation["change"].get("id"))
        if operation["change"] != normalized or operation["source"] != source:
            raise MapError("This identifier already belongs to another change.")
    else:
        operation = model.prepare(state, change, source, op_id)
    after = model.apply(state, operation)
    if len(model.encode(repo, hub, operation).encode("utf-8")) > 62000:
        raise MapError(TOO_LARGE_SEED if operation["change"].get("action") == "seed" else
                       "This change is too large for one hub comment.")
    remember(repo, hub, operation, channel="observer" if source == "observer" else "manual")
    return result(after, operation)


def holds(repo, hub, command):
    for hold in observer_store.read_holds(repo):
        if not isinstance(hold, dict):
            raise MapError("The project publication holds are unavailable.")
        if (hold.get("repo") == repo.lower() and hold.get("state") == "active"
                and hold.get("scope", {}).get("issue", hub) == hub and
                (hold.get("action") == "publish" or
                 (hold.get("action") == "command" and hold.get("command") == recortex.show_command(command)))):
            raise MapError("A user hold prevents publishing this map change.")


def commit(repo, hub, envelope, guard=None):
    if not isinstance(envelope, dict):
        raise MapError("Preview the change before applying it.")
    key = model.ident(envelope.get("id"))
    with observer_store._exclusive_lock(folder(repo, hub), "ledger.lock"):
        data = load(repo, hub, key)
        if data is None or data["operation"] != envelope or account(repo, hub) != data["account"]:
            raise MapError("Preview the change with the current project and account.")
        if (envelope["source"] == "observer" or data.get("channel") == "observer") and guard is None:
            raise MapError("Automatic publication requires observer authorization.")
        state = current(repo, hub)
        computed = model.apply(state, envelope)
        if computed is state:
            return result(state, envelope)
        if data["sending"]:
            raise MapError("The earlier send is unconfirmed. Reconcile the same preview before sending again.")
        body = model.encode(repo, hub, envelope)
        if len(body.encode("utf-8")) > 62000:
            raise MapError(TOO_LARGE_SEED if envelope["change"].get("action") == "seed" else
                           "This change is too large for one hub comment.")
        command = ["issue", "comment", str(hub), "--repo", repo, "--body-file", "-"]
        if guard:
            guard()
        if account(repo, hub) != data["account"]:
            raise MapError("The publishing account changed.")
        check_content(repo, envelope)
        holds(repo, hub, command)
        data["sending"] = True
        save(receipt_path(repo, hub, key), data)
        try:
            record_store.for_repo(repo).post_entry(hub, body=body)
        except recortex.GhError:
            pass  # the server might have accepted the body; the authoritative reread decides
        state = current(repo, hub)
        event = next((e for e in state["history"] if e["id"] == key and e["digest"] == model.digest(envelope)), None)
        if not event:
            raise MapError("No accepted entry was verified. Reconcile the same preview and review conflicts.")
        return result(state, envelope)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show")
    draft = commands.add_parser("preview")
    draft.add_argument("--change-file", required=True, help="a JSON change object, or - for stdin")
    apply = commands.add_parser("apply")
    apply.add_argument("id")
    apply.add_argument("--yes", action="store_true", required=True)
    args = parser.parse_args(argv)
    config = recortex.load_config()
    repo = recortex.resolve_repo(None, config)
    hub = recortex.resolve_hub(None, config)
    try:
        if args.command == "show":
            output = current(repo, hub)
        elif args.command == "preview":
            if args.change_file == "-":
                change = json.load(sys.stdin)
            else:
                with open(args.change_file, encoding="utf-8") as handle:
                    change = json.load(handle)
            output = preview(repo, hub, change)
        else:
            receipt = load(repo, hub, args.id)
            if not receipt:
                raise MapError("That preview was not found.")
            output = commit(repo, hub, receipt["operation"])
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (ValueError, recortex.GhError, observer_store.StoreError, storage.StorageError, OSError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
