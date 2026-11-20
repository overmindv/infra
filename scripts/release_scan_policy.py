import re
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from release_manifest import validate_plan
from release_sources import DIGEST, DOCKERFILES, SHA, UPSTREAM


SECRET_SCANNERS = {"source-secret", "image-secret"}
SCANNERS = {"go", "npm", "container", *SECRET_SCANNERS}
SEVERITIES = ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL")


@dataclass(frozen=True)
class Finding:
    scanner: str
    target: str
    id: str
    subject: str
    severity: str
    blocking: bool

    @property
    def key(self):
        return self.scanner, self.target, self.id, self.subject


def release_images(manifest):
    plan, images = manifest["plan"], manifest["images"]
    validate_plan(plan)
    if images.keys() != DOCKERFILES.keys():
        raise ValueError("release manifest must contain every published image")
    for name, ref in images.items():
        expected = f'{plan["registry"]}/{name}@'
        if not isinstance(ref, str) or not ref.startswith(expected) or not DIGEST.fullmatch(
            ref.removeprefix(expected)
        ):
            raise ValueError(f"{name}: expected published image digest from release manifest")
    return {**images, **plan["upstream"]}


def read_exceptions(entries, sources, today):
    if not isinstance(entries, list):
        raise ValueError("exceptions must be a JSON array")
    targets = {
        "go": sources - {"infra", "frontend"},
        "npm": {"frontend"},
        "container": DOCKERFILES.keys() | UPSTREAM.keys(),
        "source-secret": sources,
        "image-secret": DOCKERFILES.keys() | UPSTREAM.keys(),
    }
    allowed = {}
    fields = {
        "scanner", "target", "revision", "id", "subject", "owner", "ticket", "reason", "expires",
    }
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.keys() != fields or not all(
            isinstance(value, str) and value and value == value.strip()
            and len(value) <= 512 and value.isprintable() for value in entry.values()
        ):
            raise ValueError(f"exception {index}: expected nonempty fields {', '.join(sorted(fields))}")
        scanner, target = entry["scanner"], entry["target"]
        if scanner not in SCANNERS or target not in targets[scanner]:
            raise ValueError(f"exception {index}: unknown scanner or target")
        pattern = DIGEST if scanner in {"container", "image-secret"} else SHA
        if not pattern.fullmatch(entry["revision"]):
            raise ValueError(f"exception {index}: expected exact image digest or source SHA")
        if any("*" in entry[field] for field in ("id", "subject")):
            raise ValueError(f"exception {index}: wildcards are not allowed")
        if not re.fullmatch(r"OVM-[1-9][0-9]*", entry["ticket"]):
            raise ValueError(f"exception {index}: expected Jira key")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", entry["expires"]):
            raise ValueError(f"exception {index}: expected UTC date YYYY-MM-DD")
        end = date.fromisoformat(entry["expires"])
        if not today < end <= today + timedelta(days=30):
            raise ValueError(f"exception {index}: expired or exceeds 30 days")
        key = scanner, target, entry["id"], entry["subject"], entry["revision"]
        if key in allowed:
            raise ValueError(f"exception {index}: duplicate finding scope")
        allowed[key] = entry
    return allowed


def severity_finding(scanner, target, advisory, subject, severity):
    level = severity.upper()
    if level == "MODERATE":
        level = "MEDIUM"
    if level not in SEVERITIES:
        raise ValueError(f"{scanner}: invalid severity")
    return Finding(scanner, target, advisory, subject, level, level in {"HIGH", "CRITICAL"})


def report_findings(findings, exceptions, sources, images):
    distinct = {}
    for finding in findings:
        previous = distinct.get(finding.key)
        if previous is None or (
            finding.blocking, SEVERITIES.index(finding.severity)
        ) > (previous.blocking, SEVERITIES.index(previous.severity)):
            distinct[finding.key] = finding
    result = []
    for key, finding in sorted(distinct.items()):
        revision = (
            images[finding.target].rsplit("@", 1)[-1]
            if finding.scanner in {"container", "image-secret"}
            else sources[finding.target]
        )
        exception = exceptions.get((*key, revision))
        item = asdict(finding)
        if exception is not None:
            item["exception"] = exception
        item["decision"] = (
            "excepted" if exception else "blocked" if finding.blocking else "reported"
        )
        result.append(item)
    return result
