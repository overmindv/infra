import json

from release_scan_policy import Finding, severity_finding


def go_findings(target, output):
    config, findings = None, []
    decoder = json.JSONDecoder()
    offset = 0
    while offset < len(output):
        if output[offset].isspace():
            offset += 1
            continue
        message, offset = decoder.raw_decode(output, offset)
        if "config" in message:
            config = message["config"]
        if "finding" in message:
            item = message["finding"]
            frame = item["trace"][0]
            reachable = bool(frame.get("function"))
            findings.append(Finding(
                "go", target, item["osv"], frame["module"],
                "UNKNOWN", reachable,
            ))
    if not config or config.get("scan_level", "symbol") != "symbol":
        raise ValueError("govulncheck: missing symbol scan configuration")
    return findings


def npm_findings(target, report, exit_code):
    if (report.get("error") or report.get("auditReportVersion") != 2
            or not isinstance(report.get("vulnerabilities"), dict)):
        raise ValueError("npm audit: incomplete or unsupported report")
    findings = []
    for package, item in report["vulnerabilities"].items():
        for advisory in item["via"]:
            if isinstance(advisory, str):
                key, severity = f"via:{advisory}", item["severity"]
            else:
                key = advisory["url"].rstrip("/").rsplit("/", 1)[-1]
                severity = advisory["severity"]
            findings.append(severity_finding("npm", target, key, package, severity))
    if exit_code and not findings:
        raise ValueError("npm audit: nonzero exit without advisories")
    return findings


def source_secrets(target, report, exit_code):
    if not isinstance(report, list) or bool(report) != bool(exit_code):
        raise ValueError("gitleaks: exit code and report disagree")
    return [
        Finding("source-secret", target, item["RuleID"],
                f'{item["File"]}:{item["StartLine"]}', "CRITICAL", True)
        for item in report
    ]


def image_findings(target, report):
    if report.get("SchemaVersion") != 2 or report.get("ArtifactType") != "container_image":
        raise ValueError("trivy: incomplete or unsupported image report")
    findings = []
    for result in report.get("Results") or []:
        for item in result.get("Vulnerabilities") or []:
            findings.append(severity_finding(
                "container", target, item["VulnerabilityID"], item["PkgName"], item["Severity"],
            ))
        for item in result.get("Secrets") or []:
            findings.append(Finding(
                "image-secret", target, item["RuleID"],
                f'{result["Target"]}:{item["StartLine"]}', "CRITICAL", True,
            ))
    return findings
