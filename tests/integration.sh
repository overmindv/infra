#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

ENV_FILE="${INFRA_ENV_FILE:-./.env}"
case "$ENV_FILE" in
  /*|*/*) ;;
  *) ENV_FILE="./$ENV_FILE" ;;
esac

# env_value читает отдельную настройку без выполнения содержимого env-файла.
env_value() {
  key="$1"
  fallback="$2"
  value=""
  if [ -f "$ENV_FILE" ]; then
    value="$(awk -v key="$key" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE")"
  fi
  if [ -n "$value" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$fallback"
  fi
}

BASE_URL="$(env_value API_GATEWAY_URL http://localhost:8081)"
SUPERUSER_EMAIL="$(env_value BOOTSTRAP_SUPERUSER_EMAIL admin@overmind.v)"
SUPERUSER_PASSWORD="$(env_value BOOTSTRAP_SUPERUSER_PASSWORD zaqwsxcde)"

attempt=0
until curl -fsS "${BASE_URL}/health" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "api-gateway did not become healthy" >&2
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

student_token="$(LOGIN_RESPONSE="$login_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["LOGIN_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["login"]["token"], payload
print(payload["data"]["login"]["token"])
PY
)"

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

task_response="$(graphql 'mutation CreateITTask($input: ITTaskInput!) { createITTask(input: $input) { id status taskVersionId versionNumber options { id text isCorrect } } }' "{\"input\":{\"topicId\":\"${practice_id}\",\"title\":\"Integration HTTP test\",\"statement\":\"Which protocol is used for web pages?\",\"taskType\":\"single_choice\",\"difficulty\":\"easy\",\"options\":[{\"text\":\"HTTP\",\"isCorrect\":true},{\"text\":\"SMTP\",\"isCorrect\":false}]}}" "$admin_token")"
task_values="$(TASK_RESPONSE="$task_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["TASK_RESPONSE"])
assert not payload.get("errors"), payload
task = payload["data"]["createITTask"]
assert task["status"] == "draft", payload
assert task["versionNumber"] == 1, payload
correct = [option["id"] for option in task["options"] if option["isCorrect"]]
assert len(correct) == 1, payload
print(task["id"], task["taskVersionId"], correct[0])
PY
)"
set -- $task_values
task_id="$1"
task_version_id="$2"
correct_option_id="$3"

publish_response="$(graphql 'mutation PublishITTask($id: ID!) { changeITTaskStatus(id: $id, status: published) { id status taskVersionId } }' "{\"id\":\"${task_id}\"}" "$admin_token")"
PUBLISH_RESPONSE="$publish_response" TASK_ID="$task_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PUBLISH_RESPONSE"])
assert not payload.get("errors"), payload
task = payload["data"]["changeITTaskStatus"]
assert task["id"] == os.environ["TASK_ID"], payload
assert task["status"] == "published", payload
PY

public_task_response="$(graphql 'query ITTask($id: ID!) { itTask(id: $id) { id status options { id text isCorrect } } }' "{\"id\":\"${task_id}\"}" "$student_token")"
PUBLIC_TASK_RESPONSE="$public_task_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PUBLIC_TASK_RESPONSE"])
assert not payload.get("errors"), payload
task = payload["data"]["itTask"]
assert task["status"] == "published", payload
assert all(option["isCorrect"] is None for option in task["options"]), payload
PY

idempotency_key="$(python3 -c 'import uuid; print(uuid.uuid4())')"
submission_response="$(graphql 'mutation SubmitITTask($taskId: ID!, $input: ITSubmissionInput!) { submitITTaskAnswer(taskId: $taskId, input: $input) { id taskId taskVersionId correct verdict taskUpdated } }' "{\"taskId\":\"${task_id}\",\"input\":{\"taskVersionId\":\"${task_version_id}\",\"idempotencyKey\":\"${idempotency_key}\",\"selectedOptionIds\":[\"${correct_option_id}\"]}}" "$student_token")"
submission_id="$(SUBMISSION_RESPONSE="$submission_response" TASK_ID="$task_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["SUBMISSION_RESPONSE"])
assert not payload.get("errors"), payload
submission = payload["data"]["submitITTaskAnswer"]
assert submission["taskId"] == os.environ["TASK_ID"], payload
assert submission["correct"], payload
assert submission["verdict"] == "accepted", payload
assert not submission["taskUpdated"], payload
print(submission["id"])
PY
)"

history_response="$(graphql 'query ITSubmissionHistory($taskId: ID!) { myITSubmissions(taskId: $taskId) { items { id taskId correct verdict } } }' "{\"taskId\":\"${task_id}\"}" "$student_token")"
HISTORY_RESPONSE="$history_response" SUBMISSION_ID="$submission_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["HISTORY_RESPONSE"])
assert not payload.get("errors"), payload
items = payload["data"]["myITSubmissions"]["items"]
assert any(item["id"] == os.environ["SUBMISSION_ID"] and item["correct"] for item in items), payload
PY

programming_response="$(graphql 'mutation CreateITTask($input: ITTaskInput!) { createITTask(input: $input) { id status taskVersionId taskType } }' "{\"input\":{\"topicId\":\"${practice_id}\",\"title\":\"Integration echo\",\"statement\":\"Read one line and print it.\",\"taskType\":\"programming\",\"difficulty\":\"easy\",\"options\":[],\"tags\":[\"stdin\"],\"examples\":[{\"input\":\"hello\",\"output\":\"hello\",\"explanation\":\"Echo input\"}],\"constraints\":[\"Input is one line\"]}}" "$admin_token")"
programming_values="$(PROGRAMMING_RESPONSE="$programming_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PROGRAMMING_RESPONSE"])
assert not payload.get("errors"), payload
task = payload["data"]["createITTask"]
assert task["status"] == "draft", payload
assert task["taskType"] == "programming", payload
print(task["id"], task["taskVersionId"])
PY
)"
set -- $programming_values
programming_task_id="$1"
programming_version_id="$2"

programming_publish_response="$(graphql 'mutation PublishITTask($id: ID!) { changeITTaskStatus(id: $id, status: published) { id status } }' "{\"id\":\"${programming_task_id}\"}" "$admin_token")"
PROGRAMMING_PUBLISH_RESPONSE="$programming_publish_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["PROGRAMMING_PUBLISH_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["changeITTaskStatus"]["status"] == "published", payload
PY

code_idempotency_key="$(python3 -c 'import uuid; print(uuid.uuid4())')"
source_file="$(mktemp)"
printf '%s\n' 'value = input()' 'print(value)' >"$source_file"
operations="$(PROGRAMMING_TASK_ID="$programming_task_id" PROGRAMMING_VERSION_ID="$programming_version_id" IDEMPOTENCY_KEY="$code_idempotency_key" python3 - <<'PY'
import json, os
print(json.dumps({
    "query": "mutation SubmitITTaskCode($taskId: ID!, $input: ITCodeSubmissionInput!) { submitITTaskCode(taskId: $taskId, input: $input) { id taskId taskVersionId status executionId sourceFileName } }",
    "variables": {
        "taskId": os.environ["PROGRAMMING_TASK_ID"],
        "input": {
            "taskVersionId": os.environ["PROGRAMMING_VERSION_ID"],
            "idempotencyKey": os.environ["IDEMPOTENCY_KEY"],
            "language": "python",
            "file": None,
        },
    },
}))
PY
)"
code_submission_response="$(curl -fsS "${BASE_URL}/graphql" \
  -H "Authorization: Bearer ${student_token}" \
  -F "operations=${operations}" \
  -F 'map={"0":["variables.input.file"]}' \
  -F "0=@${source_file};type=text/x-python;filename=solution.py")"
rm -f "$source_file"

CODE_SUBMISSION_RESPONSE="$code_submission_response" PROGRAMMING_TASK_ID="$programming_task_id" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["CODE_SUBMISSION_RESPONSE"])
assert not payload.get("errors"), payload
submission = payload["data"]["submitITTaskCode"]
assert submission["taskId"] == os.environ["PROGRAMMING_TASK_ID"], payload
assert submission["status"] == "queued", payload
assert submission["executionId"], payload
assert submission["sourceFileName"] == "solution.py", payload
PY

echo "integration auth/catalog/tasks/programming submission flow: OK"
