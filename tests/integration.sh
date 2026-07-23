#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi

BASE_URL="${LASERBEAK_URL:-http://localhost:8081}"
SUPERUSER_EMAIL="${BOOTSTRAP_SUPERUSER_EMAIL:-admin@overmind.v}"
SUPERUSER_PASSWORD="${BOOTSTRAP_SUPERUSER_PASSWORD:-zaqwsxcde}"

attempt=0
until curl -fsS "${BASE_URL}/health" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "laserbeak did not become healthy" >&2
    exit 1
  fi
  sleep 2
done

suffix="$(date +%s)$$"
email="integration-${suffix}@example.com"
username="integration_${suffix}"

graphql() {
  query="$1"
  variables="$2"
  token="${3:-}"
  payload="$(QUERY="$query" VARIABLES="$variables" python3 - <<'PY'
import json, os
print(json.dumps({"query": os.environ["QUERY"], "variables": json.loads(os.environ["VARIABLES"])}))
PY
)"
  if [ -n "$token" ]; then
    curl -fsS "${BASE_URL}/graphql" -H 'Content-Type: application/json' -H "Authorization: Bearer ${token}" --data "$payload"
  else
    curl -fsS "${BASE_URL}/graphql" -H 'Content-Type: application/json' --data "$payload"
  fi
}

register_response="$(graphql 'mutation Register($input: RegisterInput!) { register(input: $input) { token user { id email username } } }' "{\"input\":{\"email\":\"${email}\",\"password\":\"password\",\"username\":\"${username}\"}}")"

student_id="$(REGISTER_RESPONSE="$register_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["REGISTER_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["register"]["token"], payload
assert payload["data"]["register"]["user"]["id"], payload
print(payload["data"]["register"]["user"]["id"])
PY
)"

login_response="$(graphql 'mutation Login($input: LoginInput!) { login(input: $input) { token user { id email roles isAdmin } } }' "{\"input\":{\"email\":\"${email}\",\"password\":\"password\"}}")"

LOGIN_RESPONSE="$login_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["LOGIN_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["login"]["token"], payload
PY

superuser_response="$(graphql 'mutation Login($input: LoginInput!) { login(input: $input) { token user { id email roles isAdmin isSuperuser } } }' "{\"input\":{\"email\":\"${SUPERUSER_EMAIL}\",\"password\":\"${SUPERUSER_PASSWORD}\"}}")"
superuser_token="$(SUPERUSER_RESPONSE="$superuser_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["SUPERUSER_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["login"]["user"]["isSuperuser"], payload
print(payload["data"]["login"]["token"])
PY
)"

promote_response="$(graphql 'mutation SetUserAdminByUsername($username: String!, $admin: Boolean!) { setUserAdminByUsername(username: $username, admin: $admin) { id username roles isAdmin } }' "{\"username\":\"${username}\",\"admin\":true}" "$superuser_token")"
PROMOTE_RESPONSE="$promote_response" STUDENT_ID="$student_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PROMOTE_RESPONSE"])
assert not payload.get("errors"), payload
user = payload["data"]["setUserAdminByUsername"]
assert user["id"] == os.environ["STUDENT_ID"], payload
assert user["isAdmin"], payload
PY

admin_login_response="$(graphql 'mutation Login($input: LoginInput!) { login(input: $input) { token user { id email roles isAdmin } } }' "{\"input\":{\"email\":\"${email}\",\"password\":\"password\"}}")"
admin_token="$(ADMIN_LOGIN_RESPONSE="$admin_login_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["ADMIN_LOGIN_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["login"]["user"]["isAdmin"], payload
print(payload["data"]["login"]["token"])
PY
)"

university_response="$(graphql 'mutation CreateUniversity($input: CreateUniversityInput!) { createUniversity(input: $input) { id name } }' '{"input":{"name":"Integration University","logoFileId":""}}' "$admin_token")"
university_id="$(UNIVERSITY_RESPONSE="$university_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["UNIVERSITY_RESPONSE"])
assert not payload.get("errors"), payload
print(payload["data"]["createUniversity"]["id"])
PY
)"

program_response="$(graphql 'mutation CreateProgram($input: CreateProgramInput!) { createProgram(input: $input) { id universityId name } }' "{\"input\":{\"universityId\":\"${university_id}\",\"name\":\"Integration Program\"}}" "$admin_token")"
program_id="$(PROGRAM_RESPONSE="$program_response" UNIVERSITY_ID="$university_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PROGRAM_RESPONSE"])
assert not payload.get("errors"), payload
program = payload["data"]["createProgram"]
assert program["universityId"] == os.environ["UNIVERSITY_ID"], payload
print(program["id"])
PY
)"

course_response="$(graphql 'mutation CreateCourse($input: CreateCourseInput!) { createCourse(input: $input) { id programId name slug } }' "{\"input\":{\"programId\":\"${program_id}\",\"name\":\"Integration Course\"}}" "$admin_token")"
course_id="$(COURSE_RESPONSE="$course_response" PROGRAM_ID="$program_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["COURSE_RESPONSE"])
assert not payload.get("errors"), payload
course = payload["data"]["createCourse"]
assert course["programId"] == os.environ["PROGRAM_ID"], payload
print(course["id"])
PY
)"

intro_response="$(graphql 'mutation CreateTopic($input: CreateTopicInput!) { createTopic(input: $input) { id courseId parentTopicId title } }' "{\"input\":{\"courseId\":\"${course_id}\",\"title\":\"Integration Intro\"}}" "$admin_token")"
intro_id="$(INTRO_RESPONSE="$intro_response" COURSE_ID="$course_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["INTRO_RESPONSE"])
assert not payload.get("errors"), payload
topic = payload["data"]["createTopic"]
assert topic["courseId"] == os.environ["COURSE_ID"], payload
print(topic["id"])
PY
)"

practice_response="$(graphql 'mutation CreateTopic($input: CreateTopicInput!) { createTopic(input: $input) { id courseId parentTopicId title } }' "{\"input\":{\"courseId\":\"${course_id}\",\"parentTopicId\":\"${intro_id}\",\"title\":\"Integration Practice\"}}" "$admin_token")"
practice_id="$(PRACTICE_RESPONSE="$practice_response" COURSE_ID="$course_id" INTRO_ID="$intro_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PRACTICE_RESPONSE"])
assert not payload.get("errors"), payload
topic = payload["data"]["createTopic"]
assert topic["courseId"] == os.environ["COURSE_ID"], payload
assert topic["parentTopicId"] == os.environ["INTRO_ID"], payload
print(topic["id"])
PY
)"

prerequisite_response="$(graphql 'mutation AddTopicPrerequisite($input: TopicPrerequisiteInput!) { addTopicPrerequisite(input: $input) { topicId prerequisiteTopicId } }' "{\"input\":{\"topicId\":\"${practice_id}\",\"prerequisiteTopicId\":\"${intro_id}\"}}" "$admin_token")"
PREREQUISITE_RESPONSE="$prerequisite_response" PRACTICE_ID="$practice_id" INTRO_ID="$intro_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PREREQUISITE_RESPONSE"])
assert not payload.get("errors"), payload
edge = payload["data"]["addTopicPrerequisite"]
assert edge["topicId"] == os.environ["PRACTICE_ID"], payload
assert edge["prerequisiteTopicId"] == os.environ["INTRO_ID"], payload
PY

echo "integration full auth/catalog flow: OK"
