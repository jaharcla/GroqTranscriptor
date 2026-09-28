import hashlib
import json
import time

import httpx


def rich(text):
    return [{"type": "text", "text": {"content": text}}]


def block_text(block):
    return "".join(
        item.get("plain_text", item.get("text", {}).get("content", ""))
        for item in block.get(block["type"], {}).get("rich_text", [])
    )


def chunks(text):
    # 1,000 code points also fit the 2,000 UTF-16-unit limit for astral characters.
    return [text[i : i + 1000] for i in range(0, len(text), 1000)]


class NotionError(RuntimeError):
    def __init__(self, status, code):
        self.status = status
        super().__init__(f"Notion HTTP {status}: {code}")


class UncertainWrite(RuntimeError):
    pass


class Notion:
    def __init__(self, config, client=None):
        self.config = config
        self.client = client or httpx.Client(
            base_url="https://api.notion.com/v1/",
            timeout=30,
            headers={"Authorization": f"Bearer {config.token}", "Notion-Version": "2025-09-03"},
        )
        self.source = config.data_source_id
        self.schema = None
        self.last_request = 0.0

    def close(self):
        self.client.close()

    def request(self, method, route, body=None):
        for attempt in range(5):
            time.sleep(max(0, 0.36 - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            response = self.client.request(method, route, json=body)
            if response.status_code in {429, 529}:
                time.sleep(max(1, float(response.headers.get("Retry-After", 2**attempt))))
                continue
            if response.is_error:
                try:
                    code = response.json().get("code", "request_failed")
                except ValueError:
                    code = "request_failed"
                # Never include response bodies: they may echo private transcript content.
                raise NotionError(response.status_code, code)
            return response.json()
        raise NotionError(429, "rate_limited")

    def prepare(self):
        if self.schema is not None:
            return
        if not self.config.token or not (self.source or self.config.database_id):
            raise ValueError(
                "Configure NOTION_TOKEN and NOTION_DATABASE_ID or NOTION_DATA_SOURCE_ID"
            )
        if not self.source:
            db = self.request("GET", f"databases/{self.config.database_id}")
            sources = db.get("data_sources", [])
            if len(sources) != 1:
                raise ValueError("Database has multiple/no data sources; set NOTION_DATA_SOURCE_ID")
            self.source = sources[0]["id"]
        schema = self.request("GET", f"data_sources/{self.source}")["properties"]
        expected = {
            "lecture": {"title"},
            "course": {"relation"},
            "date": {"date"},
            "local_path": {"rich_text"},
            "ingest_id": {"rich_text"},
            "capture": {"select", "status"},
            "processing": {"select", "status"},
        }
        for key, types in expected.items():
            name = self.config.props[key]
            if schema.get(name, {}).get("type") not in types:
                raise ValueError(
                    f"Notion property '{name}' must have type {' or '.join(sorted(types))}"
                )
        for key, value in (("capture", "Audio import"), ("processing", "Transcribed")):
            prop = schema[self.config.props[key]]
            if value not in {o["name"] for o in prop[prop["type"]].get("options", [])}:
                raise ValueError(f"Add option '{value}' to Notion '{self.config.props[key]}'")

        optional = {
            "needs_review": {"checkbox"},
            "review_flags": {"multi_select"},
            "review_flag_count": {"number"},
            "grounding_sources": {"rich_text"},
            "asr_model": {"rich_text", "select", "status"},
        }
        for key, types in optional.items():
            name = self.config.props.get(key)
            if not name or name not in schema:
                continue
            if schema[name].get("type") not in types:
                raise ValueError(
                    f"Optional Notion property '{name}' must have type "
                    f"{' or '.join(sorted(types))}"
                )
        self.schema = schema

    def query(self, filter_body):
        rows, cursor = [], None
        while True:
            body = {"filter": filter_body, "page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            result = self.request("POST", f"data_sources/{self.source}/query", body)
            rows.extend(result["results"])
            if not result.get("has_more"):
                return rows
            cursor = result["next_cursor"]

    def children(self, parent):
        result, cursor = [], None
        while True:
            route = f"blocks/{parent}/children?page_size=100"
            if cursor:
                route += f"&start_cursor={cursor}"
            page = self.request("GET", route)
            result.extend(page["results"])
            if not page.get("has_more"):
                return result
            cursor = page["next_cursor"]

    def unique(self, rows):
        if len(rows) > 1:
            raise ValueError("Multiple matching Notion rows; resolve the duplicate manually")
        return rows[0] if rows else None

    def find_key(self, key):
        return self.unique(
            self.query({"property": self.config.props["ingest_id"], "rich_text": {"equals": key}})
        )

    def recover(self, state, path):
        raw = state.get(path)["pending"]
        if not raw:
            return
        pending = json.loads(raw)
        if pending["kind"] == "page":
            recovered = self.find_key(pending["key"])
            if recovered:
                state.set(path, page=recovered["id"])
        else:
            children = self.children(pending["parent"])
            index = pending["index"]
            recovered = (
                len(children) > index
                and children[index]["type"] == pending["type"]
                and hashlib.sha256(block_text(children[index]).encode()).hexdigest()
                == pending["digest"]
            )
        if not recovered:
            raise UncertainWrite(
                "Previous write has an unknown outcome. Retry later; if still missing, inspect "
                "Notion and use reconcile <file> --confirm-not-applied. No write was repeated."
            )
        state.journal(path, None)

    def journal_write(self, state, path, pending, method, route, body):
        state.journal(path, pending)
        try:
            result = self.request(method, route, body)
        except NotionError as exc:
            if (400 <= exc.status < 500 and exc.status not in {408, 409}) or exc.status == 529:
                state.journal(path, None)
            raise
        state.journal(path, None)
        return result

    def append(self, state, path, parent, index, kind, text):
        return self.journal_write(
            state,
            path,
            {
                "kind": "append",
                "parent": parent,
                "index": index,
                "type": kind,
                "digest": hashlib.sha256(text.encode()).hexdigest(),
            },
            "PATCH",
            f"blocks/{parent}/children",
            {"children": [{"object": "block", "type": kind, kind: {"rich_text": rich(text)}}]},
        )["results"][0]

    def _review_properties(self, review_meta):
        if not review_meta:
            return {}
        p = self.config.props
        result = {}

        def schema_prop(key):
            name = p.get(key)
            return (name, self.schema.get(name)) if name and name in self.schema else (None, None)

        name, prop = schema_prop("needs_review")
        if prop and prop.get("type") == "checkbox":
            result[name] = {"checkbox": bool(review_meta.get("needs_review"))}

        name, prop = schema_prop("review_flag_count")
        if prop and prop.get("type") == "number":
            result[name] = {"number": int(review_meta.get("flag_count") or 0)}

        name, prop = schema_prop("review_flags")
        if prop and prop.get("type") == "multi_select":
            categories = []
            for value in review_meta.get("categories", []):
                value = str(value).strip()
                if value and value not in categories:
                    categories.append(value)
            result[name] = {"multi_select": [{"name": value} for value in categories[:20]]}

        name, prop = schema_prop("grounding_sources")
        if prop and prop.get("type") == "rich_text":
            sources = []
            for item in review_meta.get("grounding_sources", []):
                if isinstance(item, dict):
                    label = str(item.get("title") or item.get("id") or "").strip()
                else:
                    label = str(item).strip()
                if label and label not in sources:
                    sources.append(label)
            result[name] = {"rich_text": rich("; ".join(sources)[:1800])}

        name, prop = schema_prop("asr_model")
        if prop:
            model = str(review_meta.get("asr_model") or "").strip()
            if prop.get("type") == "rich_text":
                result[name] = {"rich_text": rich(model[:1800])}
            elif prop.get("type") in {"select", "status"} and model:
                result[name] = {prop["type"]: {"name": model}}

        return result

    def sync(self, lecture, transcript, path, state, review_meta=None):
        self.prepare()
        self.recover(state, path)
        p = self.config.props
        course = self.config.courses.get(lecture.course)
        if not course:
            raise ValueError(f"Missing course mapping: {lecture.course}")
        properties = {
            p["lecture"]: {"title": rich(lecture.title)},
            p["course"]: {"relation": [{"id": course}]},
            p["date"]: {"date": {"start": lecture.date}},
            p["local_path"]: {"rich_text": rich(str(path))},
            p["ingest_id"]: {"rich_text": rich(lecture.key)},
        }
        properties.update(self._review_properties(review_meta))
        for key, value in (("capture", "Audio import"),):
            kind = self.schema[p[key]]["type"]
            properties[p[key]] = {kind: {"name": value}}
        page = self.find_key(lecture.key)
        if not page:
            page = self.unique(
                self.query(
                    {
                        "and": [
                            {"property": p["lecture"], "title": {"equals": lecture.title}},
                            {"property": p["date"], "date": {"equals": lecture.date}},
                            {"property": p["course"], "relation": {"contains": course}},
                        ]
                    }
                )
            )
            if page:
                existing = page["properties"].get(p["ingest_id"], {}).get("rich_text", [])
                existing_key = "".join(
                    x.get("plain_text", x.get("text", {}).get("content", "")) for x in existing
                )
                if existing_key and existing_key != lecture.key:
                    raise ValueError("Matching lecture already belongs to another ingestion ID")
        if not page:
            page = self.journal_write(
                state,
                path,
                {"kind": "page", "key": lecture.key},
                "POST",
                "pages",
                {
                    "parent": {"type": "data_source_id", "data_source_id": self.source},
                    "properties": properties,
                },
            )
        page_id = page["id"]
        state.set(path, page=page_id)
        self.request("PATCH", f"pages/{page_id}", {"properties": properties})
        marker = f"CourseAI transcript {lecture.key}"
        children = self.children(page_id)
        containers = [b for b in children if b["type"] == "toggle" and block_text(b) == marker]
        container = self.unique(containers)
        if not container:
            container = self.append(state, path, page_id, len(children), "toggle", marker)
        owned = self.children(container["id"])
        if any(b["type"] != "paragraph" or b.get("has_children") for b in owned):
            raise ValueError(
                "Managed transcript contains manual blocks; move notes outside its toggle"
            )
        desired = chunks(transcript)
        for index, text in enumerate(desired):
            if index < len(owned):
                if block_text(owned[index]) != text:
                    self.request(
                        "PATCH",
                        f"blocks/{owned[index]['id']}",
                        {"paragraph": {"rich_text": rich(text)}},
                    )
            else:
                self.append(state, path, container["id"], index, "paragraph", text)
        for block in owned[len(desired) :]:
            self.request("PATCH", f"blocks/{block['id']}", {"archived": True})
        kind = self.schema[p["processing"]]["type"]
        self.request(
            "PATCH",
            f"pages/{page_id}",
            {
                "properties": {p["processing"]: {kind: {"name": "Transcribed"}}},
            },
        )
        return page_id
