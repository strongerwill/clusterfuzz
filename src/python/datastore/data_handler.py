# Copyright 2018 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Data handler functions."""

import datetime
import os
import re
import time

from base import dates
from base import errors
from base import memoize
from base import persistent_cache
from base import retry
from base import tasks
from base import utils
from config import db_config
from config import local_config
from crash_analysis import severity_analyzer
from datastore import data_types
from datastore import ndb
from datastore import ndb_utils
from google_cloud_utils import blobs
from google_cloud_utils import storage
from issue_management import issue_tracker_utils
from issue_management import label_utils
from metrics import logs
from system import environment
from system import shell

DATA_BUNDLE_DEFAULT_BUCKET_IAM_ROLE = 'roles/storage.objectAdmin'
DEFAULT_FAIL_RETRIES = 3
DEFAULT_FAIL_WAIT = 1.5
GOMA_DIR_LINE_REGEX = re.compile(r'^\s*goma_dir\s*=')
HEARTBEAT_LAST_UPDATE_KEY = 'heartbeat_update'
INPUT_DIR = 'inputs'
MEMCACHE_TTL_IN_SECONDS = 15 * 60

NUM_TESTCASE_QUALITY_BITS = 3
MAX_TESTCASE_QUALITY = 2**NUM_TESTCASE_QUALITY_BITS - 1

# Value and dimension map for some crash types (timeout, ooms).
CRASH_TYPE_VALUE_REGEX_MAP = {
    'Timeout': r'.*-timeout=(\d+)',
    'Out-of-memory': r'.*-rss_limit_mb=(\d+)',
}
CRASH_TYPE_DIMENSION_MAP = {
    'Timeout': 'secs',
    'Out-of-memory': 'MB',
}

TESTCASE_REPORT_URL = 'https://{domain}/testcase?key={testcase_id}'
TESTCASE_DOWNLOAD_URL = 'https://{domain}/download?testcase_id={testcase_id}'
TESTCASE_REVISION_RANGE_URL = (
    'https://{domain}/revisions?job={job_type}&range={revision_range}')

FILE_UNREPRODUCIBLE_TESTCASE_TEXT = (
    '<b>Note: This crash might not be reproducible with the provided testcase. '
    'That said, for the past %d days we\'ve been seeing this crash frequently. '
    'If you are unable to reproduce this, please try a speculative fix based '
    'on the crash stacktrace in the report. The fix can be verified by looking '
    'at the crash statistics in the report, a day after the fix is deployed. '
    'We will auto-close the bug if the crash is not seen for %d days.'
    '</b>' % (data_types.FILE_CONSISTENT_UNREPRODUCIBLE_TESTCASE_DEADLINE,
              data_types.UNREPRODUCIBLE_TESTCASE_WITH_BUG_DEADLINE))

# ------------------------------------------------------------------------------
# Testcase, TestcaseUploadMetadata database related functions
# ------------------------------------------------------------------------------


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def get_all_project_names():
  """Return all project names."""
  query = data_types.Testcase.query(
      projection=[data_types.Testcase.project_name],
      distinct=True).order(data_types.Testcase.project_name)
  return [
      testcase.project_name
      for testcase in query
      if testcase.project_name and testcase.project_name.strip()
  ]


def get_domain():
  """Get current domain."""
  default_domain = '{app_id}.appspot.com'.format(
      app_id=utils.get_application_id())
  return local_config.GAEConfig().get('domains.main', default=default_domain)


def get_testcase_by_id(testcase_id):
  """Return the testcase with the given id, or None if it does not exist."""
  if not testcase_id or not str(testcase_id).isdigit() or int(testcase_id) == 0:
    raise errors.InvalidTestcaseError

  testcase = ndb.Key(data_types.Testcase, int(testcase_id)).get()
  if not testcase:
    raise errors.InvalidTestcaseError

  return testcase


def find_testcase(project_name,
                  crash_type,
                  crash_state,
                  security_flag,
                  testcase_to_exclude=None):
  """Find an open test case matching certain parameters."""
  # Prepare the query.
  query = data_types.Testcase.query(
      data_types.Testcase.project_name == project_name,
      data_types.Testcase.crash_type == crash_type,
      data_types.Testcase.crash_state == crash_state,
      data_types.Testcase.security_flag == security_flag,
      data_types.Testcase.status == 'Processed',
      ndb_utils.is_true(data_types.Testcase.open))

  # Return any open (not fixed) test cases if they exist.
  testcases = ndb_utils.get_all_from_query(query)
  testcase = None
  testcase_quality = -1
  for current_testcase in testcases:
    if (testcase_to_exclude and
        current_testcase.key.id() == testcase_to_exclude.key.id()):
      continue
    if current_testcase.duplicate_of:
      continue

    # Replace the current test case in various situations where we have found
    # a better one to use. Testcase quality is based on the following factors:
    # - Is this test case reproducible? Reproducible tests are preferred.
    # - Is there a bug for this? We prefer showing tests with bugs to point
    #   users to existing bugs.
    # - Is this test case minimized ? Minimization confirms that testcase is
    #   reproducible and more usable for reproduction.
    current_testcase_quality = 0
    if not current_testcase.one_time_crasher_flag:
      current_testcase_quality |= 2**2
    if current_testcase.bug_information:
      current_testcase_quality |= 2**1
    if current_testcase.minimized_keys:
      current_testcase_quality |= 2**0

    if current_testcase_quality > testcase_quality:
      testcase = current_testcase
      testcase_quality = current_testcase_quality

    if testcase_quality == MAX_TESTCASE_QUALITY:
      # Already found the best testcase possible, no more work to do. Bail out.
      break

  return testcase


def get_crash_type_string(testcase):
  """Return a crash type string for a testcase."""
  crash_type = ' '.join(testcase.crash_type.splitlines())
  if crash_type not in CRASH_TYPE_VALUE_REGEX_MAP.keys():
    return crash_type

  crash_stacktrace = get_stacktrace(testcase)
  match = re.match(CRASH_TYPE_VALUE_REGEX_MAP[crash_type], crash_stacktrace,
                   re.DOTALL)
  if not match:
    return crash_type

  return '%s (exceeds %s %s)' % (crash_type, match.group(1),
                                 CRASH_TYPE_DIMENSION_MAP[crash_type])


def filter_stacktrace(stacktrace):
  """Filters stacktrace and returns content appropriate for storage as an
  appengine entity."""
  unicode_stacktrace = utils.decode_to_unicode(stacktrace)
  if len(unicode_stacktrace) <= data_types.STACKTRACE_LENGTH_LIMIT:
    return unicode_stacktrace

  tmpdir = environment.get_value('BOT_TMPDIR')
  tmp_stacktrace_file = os.path.join(tmpdir, 'stacktrace.tmp')

  try:
    with open(tmp_stacktrace_file, 'w') as handle:
      handle.write(stacktrace)
    with open(tmp_stacktrace_file, 'r') as handle:
      key = blobs.write_blob(handle)
  except Exception:
    logs.log_error('Unable to write crash stacktrace to temporary file.')
    shell.remove_file(tmp_stacktrace_file)
    return unicode_stacktrace[(-1 * data_types.STACKTRACE_LENGTH_LIMIT):]

  shell.remove_file(tmp_stacktrace_file)
  return '%s%s' % (data_types.BLOBSTORE_STACK_PREFIX, key)


def get_issue_summary(testcase):
  """Gets an issue description string for a testcase.

  It is used in bug description.
  """
  # Get summary prefix. Note that values for fuzzers take priority over those
  # from job definitions.
  fuzzer_summary_prefix = get_value_from_fuzzer_environment_string(
      testcase.fuzzer_name, 'SUMMARY_PREFIX')
  job_summary_prefix = get_value_from_job_definition(testcase.job_type,
                                                     'SUMMARY_PREFIX')
  summary_prefix = fuzzer_summary_prefix or job_summary_prefix or ''

  if summary_prefix:
    binary_name = testcase.get_metadata('fuzzer_binary_name')
    if binary_name and get_project_name(testcase.job_type) == summary_prefix:
      summary_prefix += '/' + binary_name

    summary_prefix += ': '

  # No crash state.
  if testcase.crash_state == 'NULL':
    return summary_prefix + 'NULL'

  # Special case for bad-cast style testcases.
  if testcase.crash_type.startswith('Bad-cast'):
    issue_summary = summary_prefix
    crash_state_lines = testcase.crash_state.splitlines()
    if crash_state_lines:
      # Add the to/from line if available.
      issue_summary += crash_state_lines[0]
    if len(crash_state_lines) > 1:
      # Add the crash function if available.
      issue_summary += ' in ' + crash_state_lines[1]
    return issue_summary

  # For ASSERTs and CHECK failures, we should just use the crash type and the
  # first line of the crash state as titles. Note that ASSERT_NOT_REACHED should
  # be handled by the general case.
  if testcase.crash_type in [
      'ASSERT', 'CHECK failure', 'Security CHECK failure',
      'Security DCHECK failure'
  ]:
    return (summary_prefix + testcase.crash_type + ': ' +
            testcase.crash_state.splitlines()[0])

  # Add first lines from crash type and crash_state.
  issue_summary = ''
  if testcase.crash_type:
    filtered_crash_type = testcase.crash_type.splitlines()[0]
    filtered_crash_type = re.sub(r'UNKNOWN( READ| WRITE)?', 'Crash',
                                 filtered_crash_type)
    issue_summary += filtered_crash_type
  if testcase.crash_state:
    issue_summary += ' in ' + testcase.crash_state.splitlines()[0]

  if not issue_summary:
    issue_summary = '<no crash state available>'

  return summary_prefix + issue_summary


def get_reproduction_help_url(testcase, config):
  """Return url to reproduce the bug."""
  return get_value_from_job_definition(
      testcase.job_type, 'HELP_URL', default=config.reproduction_help_url)


def get_issue_description(testcase, reporter=None, show_reporter=False):
  """Returns testcase as string."""
  # Get issue tracker configuration parameters.
  config = db_config.get()
  domain = get_domain()
  testcase_id = testcase.key.id()

  fuzzer_name = testcase.overridden_fuzzer_name or testcase.fuzzer_name
  download_url = TESTCASE_DOWNLOAD_URL.format(
      domain=domain, testcase_id=testcase_id)
  report_url = TESTCASE_REPORT_URL.format(
      domain=domain, testcase_id=testcase_id)
  regressed_revision_range_url = TESTCASE_REVISION_RANGE_URL.format(
      domain=domain,
      job_type=testcase.job_type,
      revision_range=testcase.regression)
  fixed_revision_range_url = TESTCASE_REVISION_RANGE_URL.format(
      domain=domain, job_type=testcase.job_type, revision_range=testcase.fixed)

  if testcase.status == 'Unreproducible':
    return ('Testcase {testcase_id} failed to reproduce the crash. '
            'Please inspect the program output at {report_url}.'.format(
                testcase_id=testcase_id, report_url=report_url))

  # Now create the content string.
  content_string = 'Detailed report: %s\n\n' % report_url

  project_name = get_project_name(testcase.job_type)
  if project_name and project_name != utils.default_project_name():
    content_string += 'Project: %s\n' % project_name

  if fuzzer_name:
    content_string += 'Fuzzer: %s\n' % fuzzer_name
    binary_name = testcase.get_metadata('fuzzer_binary_name')
    if binary_name:
      content_string += 'Fuzz target binary: %s\n' % binary_name

  content_string += 'Job Type: %s\n' % testcase.job_type

  # Add platform id if other than default ones. Only applicable to Android.
  # e.g. android:shamu_asan
  if testcase.platform_id:
    content_string += 'Platform Id: %s\n\n' % testcase.platform_id

  content_string += 'Crash Type: %s\n' % get_crash_type_string(testcase)
  content_string += 'Crash Address: %s\n' % testcase.crash_address
  content_string += 'Crash State:\n%s\n' % (
      utils.indent_string(testcase.crash_state + '\n', 2))

  content_string += '%s\n\n' % environment.get_memory_tool_display_string(
      testcase.job_type)

  if data_types.SecuritySeverity.is_valid(testcase.security_severity):
    content_string += (
        'Recommended Security Severity: %s\n\n' %
        label_utils.severity_to_string(testcase.security_severity))

  if (testcase.regression and testcase.regression != 'NA' and
      not testcase.regression.startswith('0:') and
      not testcase.regression.endswith('!')):
    content_string += 'Regressed: %s\n' % regressed_revision_range_url

  if (testcase.fixed and testcase.fixed != 'NA' and testcase.fixed != 'Yes' and
      not testcase.fixed.endswith('!')):
    content_string += 'Fixed: %s\n' % fixed_revision_range_url

  if not content_string.endswith('\n\n'):
    content_string += '\n'

  content_string += 'Reproducer Testcase: %s\n\n' % download_url

  second_crash_stacktrace = get_stacktrace(
      testcase, stack_attribute='second_crash_stacktrace')
  if testcase.one_time_crasher_flag and second_crash_stacktrace:
    content_string += (second_crash_stacktrace.split('\n'))[0] + '\n\n'

  if testcase.gestures:
    content_string += 'Additional requirements: Requires Gestures\n\n'
  if testcase.http_flag:
    content_string += 'Additional requirements: Requires HTTP\n\n'

  if show_reporter:
    if reporter:
      content_string += (
          'Issue manually filed by: %s\n\n' % reporter.split('@')[0])
    else:
      content_string += 'Issue filed automatically.\n\n'

  # Jobs can override the help url.
  content_string += 'See %s for instructions to reproduce this bug locally.' % (
      get_reproduction_help_url(testcase, config))

  # Unreproducible crash text is only applicable when we are consistently seeing
  # it happening, and hence the reason for auto-filing it. Otherwise, someone
  # filed it manually, so skip the text in that case.
  if not reporter and testcase.one_time_crasher_flag:
    content_string += '\n\n' + FILE_UNREPRODUCIBLE_TESTCASE_TEXT

  return content_string


def get_stacktrace(testcase, stack_attribute='crash_stacktrace'):
  """Returns the stacktrace for a test case.

  This may require a blobstore read.
  """
  result = getattr(testcase, stack_attribute)
  if not result or not result.startswith(data_types.BLOBSTORE_STACK_PREFIX):
    return result

  # For App Engine, we can't write to local file, so use blobs.read_key instead.
  if environment.is_running_on_app_engine():
    key = result[len(data_types.BLOBSTORE_STACK_PREFIX):]
    return unicode(blobs.read_key(key), errors='replace')

  key = result[len(data_types.BLOBSTORE_STACK_PREFIX):]
  tmpdir = environment.get_value('BOT_TMPDIR')
  tmp_stacktrace_file = os.path.join(tmpdir, 'stacktrace.tmp')
  blobs.read_blob_to_disk(key, tmp_stacktrace_file)

  try:
    handle = open(tmp_stacktrace_file)
    result = handle.read()
    handle.close()
  except:
    logs.log_error(
        'Unable to read stacktrace for testcase %d.' % testcase.key.id())
    result = ''

  shell.remove_file(tmp_stacktrace_file)
  return result


def handle_duplicate_entry(testcase):
  """Handles duplicates and deletes unreproducible one."""
  # Caller ensures that our testcase object is up-to-date. If someone else
  # already marked us as a duplicate, no more work to do.
  if testcase.duplicate_of:
    return

  existing_testcase = find_testcase(
      testcase.project_name,
      testcase.crash_type,
      testcase.crash_state,
      testcase.security_flag,
      testcase_to_exclude=testcase)
  if not existing_testcase:
    return

  # If the existing testcase's minimization has not completed yet, we shouldn't
  # be doing the next step. The testcase might turn out to be a non reproducible
  # bug and we don't want to delete the other testcase which could be a fully
  # minimized and reproducible bug.
  if not existing_testcase.minimized_keys:
    return

  testcase_id = testcase.key.id()
  existing_testcase_id = existing_testcase.key.id()
  if (not testcase.bug_information and
      not existing_testcase.one_time_crasher_flag):
    metadata = data_types.TestcaseUploadMetadata.query(
        data_types.TestcaseUploadMetadata.testcase_id == testcase_id).get()
    if metadata:
      metadata.status = 'Duplicate'
      metadata.duplicate_of = existing_testcase_id
      metadata.security_flag = existing_testcase.security_flag
      metadata.put()

    testcase.status = 'Duplicate'
    testcase.duplicate_of = existing_testcase_id
    testcase.put()
    logs.log('Marking testcase %d as duplicate of testcase %d.' %
             (testcase_id, existing_testcase_id))

  elif (not existing_testcase.bug_information and
        not testcase.one_time_crasher_flag):
    metadata = data_types.TestcaseUploadMetadata.query(
        data_types.TestcaseUploadMetadata.testcase_id == testcase_id).get()
    if metadata:
      metadata.status = 'Duplicate'
      metadata.duplicate_of = testcase_id
      metadata.security_flag = testcase.security_flag
      metadata.put()

    existing_testcase.status = 'Duplicate'
    existing_testcase.duplicate_of = testcase_id
    existing_testcase.put()
    logs.log('Marking testcase %d as duplicate of testcase %d.' %
             (existing_testcase_id, testcase_id))


def is_first_retry_for_task(testcase, reset_after_retry=False):
  """Returns true if this task is tried atleast once. Only applicable for
  analyze and progression tasks."""
  task_name = environment.get_value('TASK_NAME')
  retry_key = '%s_retry' % task_name
  retry_flag = testcase.get_metadata(retry_key)
  if not retry_flag:
    # Update the metadata key since now we have tried it once.
    retry_value = True
    testcase.set_metadata(retry_key, retry_value)
    return True

  # Reset the metadata key so that tasks like progression task can be retried.
  if reset_after_retry:
    retry_value = False
    testcase.set_metadata(retry_key, retry_value)

  return False


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def get_issue_tracker_name(job_type=None):
  """Return issue tracker name for a job type."""
  default_issue_tracker_name = environment.get_value('ISSUE_TRACKER')

  if not job_type:
    return default_issue_tracker_name

  return get_value_from_job_definition(job_type, 'ISSUE_TRACKER',
                                       default_issue_tracker_name)


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def get_project_name(job_type):
  """Return project name for a job type."""
  project_name = get_value_from_job_definition(job_type, 'PROJECT_NAME')
  if project_name:
    return project_name

  return utils.default_project_name()


def _get_security_severity(crash, job_type, gestures):
  """Get security severity."""
  if crash.security_flag:
    return severity_analyzer.get_security_severity(
        crash.crash_type, crash.crash_stacktrace, job_type, bool(gestures))

  return None


def store_testcase(crash, fuzzed_keys, minimized_keys, regression, fixed,
                   one_time_crasher_flag, crash_revision, comment,
                   absolute_path, fuzzer_name, fully_qualified_fuzzer_name,
                   job_type, archived, archive_filename, binary_flag, http_flag,
                   gestures, redzone, minidump_keys, window_argument,
                   timeout_multiplier, minimized_arguments):
  """Create a testcase and store it in the datastore using remote api."""
  # Initialize variable to prevent invalid values.
  if archived:
    archive_state = data_types.ArchiveStatus.FUZZED
  else:
    archive_state = 0
  if not gestures:
    gestures = []
  if not redzone:
    redzone = 128

  # Create the testcase.
  testcase = data_types.Testcase()
  testcase.crash_type = crash.crash_type
  testcase.crash_address = crash.crash_address
  testcase.crash_state = utils.decode_to_unicode(crash.crash_state)
  testcase.crash_stacktrace = filter_stacktrace(crash.crash_stacktrace)
  testcase.fuzzed_keys = fuzzed_keys
  testcase.minimized_keys = minimized_keys
  testcase.bug_information = ''
  testcase.regression = regression
  testcase.fixed = fixed
  testcase.security_flag = crash.security_flag
  testcase.security_severity = _get_security_severity(crash, job_type, gestures)

  testcase.one_time_crasher_flag = one_time_crasher_flag
  testcase.crash_revision = crash_revision
  testcase.original_absolute_path = absolute_path
  testcase.absolute_path = absolute_path
  testcase.fuzzer_name = fuzzer_name
  testcase.overridden_fuzzer_name = fully_qualified_fuzzer_name or fuzzer_name
  testcase.job_type = job_type
  testcase.queue = tasks.default_queue()
  testcase.archive_state = archive_state
  testcase.archive_filename = archive_filename
  testcase.binary_flag = binary_flag
  testcase.http_flag = http_flag
  testcase.timestamp = datetime.datetime.utcnow()
  testcase.gestures = gestures
  testcase.redzone = redzone
  testcase.minidump_keys = minidump_keys
  testcase.window_argument = window_argument
  testcase.timeout_multiplier = float(timeout_multiplier)
  testcase.minimized_arguments = minimized_arguments
  testcase.project_name = get_project_name(job_type)

  # Set metadata fields (e.g. build url, build key, platform string, etc).
  set_initial_testcase_metadata(testcase)

  # Update the comment and save testcase.
  update_testcase_comment(testcase, data_types.TaskState.NA, comment)

  # Get testcase id from newly created testcase.
  testcase_id = testcase.key.id()
  logs.log(
      ('Created new testcase %d (reproducible:%s, security:%s, binary:%s).\n'
       'crash_type: %s\ncrash_state:\n%s\n') %
      (testcase_id, not testcase.one_time_crasher_flag, testcase.security_flag,
       testcase.binary_flag, testcase.crash_type, testcase.crash_state))

  # Update global blacklist to avoid finding this leak again (if needed).
  is_lsan_enabled = environment.get_value('LSAN')
  if is_lsan_enabled:
    from fuzzing import leak_blacklist
    leak_blacklist.add_crash_to_global_blacklist_if_needed(testcase)

  return testcase_id


def set_initial_testcase_metadata(testcase):
  """Set various testcase metadata fields during testcase initialization."""
  build_key = environment.get_value('BUILD_KEY')
  if build_key:
    testcase.set_metadata('build_key', build_key, update_testcase=False)

  build_url = environment.get_value('BUILD_URL')
  if build_url:
    testcase.set_metadata('build_url', build_url, update_testcase=False)

  gn_args_path = environment.get_value('GN_ARGS_PATH', '')
  if gn_args_path and os.path.exists(gn_args_path):
    gn_args = utils.read_data_from_file(
        gn_args_path, eval_data=False, default='')

    # Remove goma_dir from gn args since it is only relevant to the machine that
    # did the build.
    filtered_gn_args_lines = [
        line for line in gn_args.splitlines()
        if not GOMA_DIR_LINE_REGEX.match(line)
    ]
    filtered_gn_args = '\n'.join(filtered_gn_args_lines)
    testcase.set_metadata('gn_args', filtered_gn_args, update_testcase=False)

  testcase.platform = environment.platform().lower()
  testcase.platform_id = environment.get_platform_id()


def update_testcase_comment(testcase, task_state, message=None):
  """Add task status and message to the test case's comment field."""
  bot_name = environment.get_value('BOT_NAME', 'Unknown')
  task_name = environment.get_value('TASK_NAME', 'Unknown')
  task_string = '%s task' % task_name.capitalize()
  timestamp = utils.current_date_time()

  # For some tasks like blame, progression and impact, we need to delete lines
  # from old task executions to avoid clutter.
  if (task_name in ['blame', 'progression', 'impact'] and
      task_state == data_types.TaskState.STARTED):
    pattern = r'.*?: %s.*\n' % task_string
    testcase.comments = re.sub(pattern, '', testcase.comments)

  testcase.comments += '[%s] %s: %s %s' % (timestamp, bot_name, task_string,
                                           task_state)
  if message:
    testcase.comments += ': %s' % message
  testcase.comments += '.\n'
  testcase.put()

  # Log the message in stackdriver after the testcase.put() call as otherwise
  # the testcase key might not available yet (i.e. for new testcase).
  if message:
    log_func = (
        logs.log_error
        if task_state == data_types.TaskState.ERROR else logs.log)
    log_func('%s (testcase %s, job %s).' % (message, testcase.key.id(),
                                            testcase.job_type))


def get_open_testcase_id_iterator():
  """Get an iterator for open testcase ids."""
  keys = ndb_utils.get_all_from_query(
      data_types.Testcase.query(
          ndb_utils.is_true(data_types.Testcase.open),
          data_types.Testcase.status == 'Processed'),
      keys_only=True,
      batch_size=data_types.TESTCASE_ENTITY_QUERY_LIMIT)
  for key in keys:
    yield key.id()


def critical_tasks_completed(testcase):
  """Check to see if all critical tasks have finished running on a test case."""
  if testcase.status == 'Unreproducible':
    # These tasks don't apply to unreproducible testcases.
    return True

  if testcase.one_time_crasher_flag:
    # These tasks don't apply to flaky testcases.
    return True

  # For non-chromium projects, impact and blame tasks are not applicable.
  if not utils.is_chromium():
    return testcase.minimized_keys and testcase.regression

  return bool(testcase.minimized_keys and testcase.regression and
              testcase.is_impact_set_flag)


# ------------------------------------------------------------------------------
# BuildMetadata database related functions
# ------------------------------------------------------------------------------


def get_build_state(job_type, crash_revision):
  """Return whether a build is unmarked, good or bad."""
  build = data_types.BuildMetadata.query(
      data_types.BuildMetadata.job_type == job_type,
      data_types.BuildMetadata.revision == crash_revision).get()

  if not build:
    return data_types.BuildState.UNMARKED

  if build.bad_build:
    return data_types.BuildState.BAD

  return data_types.BuildState.GOOD


def add_build_metadata(job_type,
                       crash_revision,
                       is_bad_build,
                       console_output=None):
  """Add build metadata."""
  build = data_types.BuildMetadata()
  build.bad_build = is_bad_build
  build.bot_name = environment.get_value('BOT_NAME')
  build.console_output = console_output
  build.job_type = job_type
  build.revision = crash_revision
  build.timestamp = datetime.datetime.utcnow()
  build.put()

  if is_bad_build:
    logs.log_error(
        'Bad build %s.' % job_type,
        revision=crash_revision,
        job_type=job_type,
        output=console_output)
  else:
    logs.log(
        'Good build %s.' % job_type, revision=crash_revision, job_type=job_type)
  return build


# Fuzzer and DataBundle database related functions
# ------------------------------------------------------------------------------


def create_data_bundle_bucket_and_iams(data_bundle_name, emails):
  """Creates a data bundle bucket and adds iams for access."""
  bucket_name = get_data_bundle_bucket_name(data_bundle_name)
  if not storage.create_bucket_if_needed(bucket_name):
    return False

  client = storage.create_discovery_storage_client()
  iam_policy = storage.get_bucket_iam_policy(client, bucket_name)
  if not iam_policy:
    return False

  members = []

  # Add access for the domains allowed in project.
  domains = local_config.AuthConfig().get('whitelisted_domains', default=[])
  for domain in domains:
    members.append('domain:%s' % domain)

  # Add access for the emails provided in function arguments.
  for email in emails:
    members.append('user:%s' % email)

  if not members:
    # No members to add, bail out.
    return True

  binding = storage.get_bucket_iam_binding(iam_policy,
                                           DATA_BUNDLE_DEFAULT_BUCKET_IAM_ROLE)
  if binding:
    binding['members'] = members
  else:
    binding = {
        'role': DATA_BUNDLE_DEFAULT_BUCKET_IAM_ROLE,
        'members': members,
    }
    iam_policy['bindings'].append(binding)

  return bool(storage.set_bucket_iam_policy(client, bucket_name, iam_policy))


def get_data_bundle_bucket_name(data_bundle_name):
  """Return data bundle bucket name on GCS."""
  domain = '%s.appspot.com' % utils.get_application_id()
  return '%s-corpus.%s' % (data_bundle_name, domain)


def get_data_bundle_bucket_url(data_bundle_name):
  """Return data bundle bucket url on GCS."""
  return 'gs://%s' % get_data_bundle_bucket_name(data_bundle_name)


def get_value_from_fuzzer_environment_string(fuzzer_name,
                                             variable_pattern,
                                             default=None):
  """Get a specific environment variable's value for a fuzzer."""
  fuzzer = data_types.Fuzzer.query(data_types.Fuzzer.name == fuzzer_name).get()
  if not fuzzer or not fuzzer.additional_environment_string:
    return default

  return get_value_from_environment_string(
      fuzzer.additional_environment_string, variable_pattern, default=default)


# ------------------------------------------------------------------------------
# TaskStatus database related functions
# ------------------------------------------------------------------------------


def get_task_status(name, create_if_needed=False):
  """Return the TaskStatus object with the given name."""
  metadata = ndb.Key(data_types.TaskStatus, name).get()
  if not metadata and create_if_needed:
    metadata = data_types.TaskStatus(id=name)

  return metadata


def update_task_status(task_name, status, expiry_interval=None):
  """Updates status for a task. Used to ensure that a single instance of a task
  is running at any given time."""
  bot_name = environment.get_value('BOT_NAME')
  failure_wait_interval = environment.get_value('FAIL_WAIT')

  # If we didn't get an expiry interval, default to our task lease interval.
  if expiry_interval is None:
    expiry_interval = environment.get_value('TASK_LEASE_SECONDS')
    if expiry_interval is None:
      logs.log_error('expiry_interval is None and TASK_LEASE_SECONDS not set.')

  def _try_update_status():
    """Try update metadata."""
    task_status = get_task_status(task_name, create_if_needed=True)

    # If another bot is already working on this task, bail out with error.
    if (status == data_types.TaskState.STARTED and
        task_status.status == data_types.TaskState.STARTED and
        not dates.time_has_expired(
            task_status.time, seconds=expiry_interval - 1)):
      return False

    task_status.bot_name = bot_name
    task_status.status = status
    task_status.time = utils.utcnow()
    task_status.put()
    return True

  # It is important that we do not continue until the metadata is updated.
  # This can lead to task loss, or can cause issues with multiple bots
  # attempting to run the task at the same time.
  while True:
    try:
      return ndb.transaction(_try_update_status, retries=0)
    except Exception:
      # We need to update the status under all circumstances.
      # Failing to update 'completed' status causes another bot
      # that picked up this job to bail out.
      logs.log_error('Unable to update %s task metadata. Retrying.' % task_name)
      time.sleep(utils.random_number(1, failure_wait_interval))


# ------------------------------------------------------------------------------
# Heartbeat database related functions
# ------------------------------------------------------------------------------


def update_heartbeat():
  """Updates heartbeat with current timestamp and log data."""
  # Check if the heartbeat was recently updated. If yes, bail out.
  last_modified_time = persistent_cache.get_value(
      HEARTBEAT_LAST_UPDATE_KEY, constructor=datetime.datetime.utcfromtimestamp)
  if (last_modified_time is not None and not dates.time_has_expired(
      last_modified_time, seconds=data_types.HEARTBEAT_WAIT_INTERVAL)):
    return 0

  bot_name = environment.get_value('BOT_NAME')
  current_time = datetime.datetime.utcnow()

  try:
    # TODO(ochang): Get heartbeat by key and remove duplicate cleanup once
    # entities are updated.
    heartbeats = ndb_utils.get_all_from_query(
        data_types.Heartbeat.query(data_types.Heartbeat.bot_name == bot_name))

    # Remove all heartbeat objects other than the first one. This is rare unless
    # both bot and heartbeat scripts update the heartbeat at exactly the same
    # time.
    heartbeats_to_delete = list(heartbeats)
    if heartbeats_to_delete:
      heartbeat = heartbeats_to_delete.pop()

      if heartbeats_to_delete:
        ndb.delete_multi([heartbeat.key for heartbeat in heartbeats_to_delete])
    else:
      heartbeat = data_types.Heartbeat()
      heartbeat.bot_name = bot_name

    heartbeat.key = ndb.Key(data_types.Heartbeat, bot_name)
    heartbeat.task_payload = tasks.get_task_payload()
    heartbeat.task_end_time = tasks.get_task_end_time()
    heartbeat.last_beat_time = current_time
    heartbeat.source_version = utils.current_source_version()
    heartbeat.put()

    persistent_cache.set_value(
        HEARTBEAT_LAST_UPDATE_KEY, time.time(), persist_across_reboots=True)
  except:
    logs.log_error('Unable to update heartbeat.')
    return 0

  return 1


def bot_run_timed_out():
  """Return true if our run timed out."""
  run_timeout = environment.get_value('RUN_TIMEOUT')
  if not run_timeout:
    return False

  # Check that we have a valid start time from our heartbeat.
  bot_name = environment.get_value('BOT_NAME')
  heartbeat = data_types.Heartbeat.query(
      data_types.Heartbeat.bot_name == bot_name).get()
  if not heartbeat or not heartbeat.start_time:
    return False

  # Actual run timeout takes off the duration for one task.
  average_task_duration = environment.get_value('AVERAGE_TASK_DURATION', 0)
  actual_run_timeout = run_timeout - average_task_duration

  return dates.time_has_expired(
      heartbeat.start_time, seconds=actual_run_timeout)


# ------------------------------------------------------------------------------
# Job database related functions
# ------------------------------------------------------------------------------


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def get_component_name(job_type):
  """Gets component name for a job type."""
  job = data_types.Job.query(data_types.Job.name == job_type).get()
  if not job:
    return ''

  match = re.match(r'.*BUCKET_PATH[^\r\n]*-([a-zA-Z0-9]+)-component',
                   job.get_environment_string(), re.DOTALL)
  if not match:
    return ''

  component_name = match.group(1)
  return component_name


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def get_repository_for_component(component):
  """Get the repository based on component."""
  default_repository = ''
  repository = ''
  repository_mappings = db_config.get_value('component_repository_mappings')

  for line in repository_mappings.splitlines():
    current_component, value = line.split(';', 1)

    if current_component == 'default':
      default_repository = value
    elif current_component == component:
      repository = value

  return repository or default_repository


def get_value_from_environment_string(environment_string,
                                      variable_pattern,
                                      default=None):
  """Return the first value matching the pattern from the environment string."""
  pattern = r'%s\s*=\s*(.*)' % variable_pattern
  match = re.search(pattern, environment_string)
  if not match:
    return default

  return match.group(1).strip()


def get_value_from_job_definition(job_type, variable_pattern, default=None):
  """Get a specific environment variable's value from a job definition."""
  job = data_types.Job.query(data_types.Job.name == job_type).get()
  if not job:
    return default

  return job.get_environment().get(variable_pattern, default)


def get_additional_values_for_variable(variable_name, job_type, fuzzer_name):
  """Helper function to read a list of additional items from a job definition
     and fuzzer's additional environment string."""
  value_list_strings = [
      get_value_from_job_definition(job_type, variable_name),
      get_value_from_fuzzer_environment_string(fuzzer_name, variable_name),
  ]

  additional_values = []
  for value_list_string in value_list_strings:
    if value_list_string:
      # Ignore whitespace between commas.
      additional_values += [v.strip() for v in value_list_string.split(',')]

  return additional_values


# ------------------------------------------------------------------------------
# Notification database related functions
# ------------------------------------------------------------------------------


def is_notification_sent(testcase_id, user_email):
  """Return true if this notification has already been sent."""
  notification = data_types.Notification.query(
      data_types.Notification.testcase_id == testcase_id,
      data_types.Notification.user_email == user_email).get()
  return bool(notification)


def create_notification_entry(testcase_id, user_email):
  """Create a entry log for sent notification."""
  notification = data_types.Notification()
  notification.testcase_id = testcase_id
  notification.user_email = user_email
  notification.put()


def update_issue_impact_labels(testcase, issue):
  """Update impact labels on issue."""
  if testcase.one_time_crasher_flag:
    return

  if get_component_name(testcase.job_type):
    # Component builds are not supported.
    return

  existing_impact = label_utils.get_impact_from_labels(
      [label.lower() for label in issue.labels])

  if testcase.regression.startswith('0:'):
    # If the regression range starts from the start of time,
    # then we assume that the bug impacts stable.
    new_impact = data_types.SecurityImpact.STABLE
  elif testcase.is_impact_set_flag:
    # Add impact label based on testcase's impact value.
    if testcase.impact_stable_version:
      new_impact = data_types.SecurityImpact.STABLE
    elif testcase.impact_beta_version:
      new_impact = data_types.SecurityImpact.BETA
    else:
      new_impact = data_types.SecurityImpact.HEAD
  else:
    # No impact information.
    return

  if existing_impact == new_impact:
    # Correct impact already set.
    return

  if existing_impact != data_types.SecurityImpact.MISSING:
    issue.remove_label('Security_Impact-' +
                       label_utils.impact_to_string(existing_impact))

  issue.add_label('Security_Impact-' + label_utils.impact_to_string(new_impact))


def update_issue_severity_labels(testcase, issue):
  """Update severity labels on issue."""
  if not data_types.SecuritySeverity.is_valid(testcase.security_severity):
    return

  issue_severity = label_utils.get_severity_from_labels(
      [label.lower() for label in issue.labels])
  recommended_severity = label_utils.severity_to_label(
      testcase.security_severity)

  if issue_severity == data_types.SecuritySeverity.MISSING:
    issue.add_label(recommended_severity)
    issue.comment += ('\n\nA recommended severity was added to this bug. '
                      'Please change the severity if it is inaccurate.')
  elif issue_severity != testcase.security_severity:
    issue.comment += (
        '\n\nThe recommended severity (%s) is different from what was assigned '
        'to the bug. Please double check the accuracy of the assigned '
        'severity.' % recommended_severity)


def get_issue_for_testcase(testcase):
  """Return issue associated with the testcase (if any)."""
  if not testcase.bug_information:
    return None

  issue_id = int(testcase.bug_information)
  itm = issue_tracker_utils.get_issue_tracker_manager(testcase)

  try:
    issue = itm.get_issue(issue_id)
  except Exception:
    logs.log_error('Unable to query issue %d.' % issue_id)
    return None

  return issue


# ------------------------------------------------------------------------------
# TestcaseUploadMetadata database related functions
# ------------------------------------------------------------------------------


def create_user_uploaded_testcase(key,
                                  original_key,
                                  archive_state,
                                  filename,
                                  file_path_input,
                                  timeout,
                                  job_type,
                                  queue,
                                  http_flag,
                                  gestures,
                                  additional_arguments,
                                  bug_information,
                                  crash_revision,
                                  uploader_email,
                                  platform_id,
                                  app_launch_command,
                                  fuzzer_name,
                                  fully_qualified_fuzzer_name,
                                  fuzzer_binary_name,
                                  bundled,
                                  retries,
                                  bug_summary_update_flag,
                                  additional_metadata=None):
  """Create a testcase object, metadata, and task for a user uploaded test."""
  testcase = data_types.Testcase()
  testcase.crash_type = ''
  testcase.crash_state = 'Pending'
  testcase.crash_address = ''
  testcase.crash_stacktrace = ''
  testcase.fuzzed_keys = key
  testcase.minimized_keys = ''
  testcase.bug_information = ''
  testcase.regression = ''
  testcase.fixed = ''
  testcase.security_flag = False
  testcase.one_time_crasher_flag = False
  testcase.crash_revision = crash_revision
  testcase.comments = '[%s] %s: Analyze task.\n' % (utils.current_date_time(),
                                                    uploader_email)
  testcase.fuzzer_name = fuzzer_name
  testcase.overridden_fuzzer_name = fully_qualified_fuzzer_name or fuzzer_name
  testcase.job_type = job_type
  testcase.http_flag = bool(http_flag)
  testcase.archive_state = archive_state
  testcase.status = 'Pending'
  testcase.project_name = get_project_name(job_type)

  if archive_state or bundled:
    testcase.absolute_path = file_path_input
    testcase.archive_filename = filename
  else:
    testcase.absolute_path = filename
  testcase.gestures = gestures
  if bug_information:
    testcase.bug_information = bug_information
  if platform_id:
    testcase.platform_id = platform_id.strip().lower()
  if additional_arguments:
    testcase.set_metadata(
        'uploaded_additional_args', additional_arguments, update_testcase=False)
  if app_launch_command:
    testcase.set_metadata(
        'app_launch_command', app_launch_command, update_testcase=False)
  if fuzzer_binary_name:
    testcase.set_metadata(
        'fuzzer_binary_name', fuzzer_binary_name, update_testcase=False)

  if additional_metadata:
    for metadata_key, metadata_value in additional_metadata.iteritems():
      testcase.set_metadata(metadata_key, metadata_value, update_testcase=False)

  testcase.timestamp = datetime.datetime.utcnow()
  testcase.uploader_email = uploader_email
  testcase.put()

  # Store the testcase upload metadata.
  testcase_id = testcase.key.id()
  metadata = data_types.TestcaseUploadMetadata()
  metadata.security_flag = False
  metadata.filename = filename
  metadata.status = 'Pending'
  metadata.uploader_email = uploader_email
  metadata.testcase_id = testcase_id
  metadata.blobstore_key = key
  metadata.original_blobstore_key = original_key
  metadata.timeout = timeout
  metadata.bundled = bundled
  metadata.retries = retries
  if bundled:
    metadata.path_in_archive = file_path_input
  metadata.timestamp = testcase.timestamp
  metadata.bug_summary_update_flag = bool(bug_summary_update_flag)
  metadata.put()

  # Create the job to analyze the testcase.
  tasks.add_task('analyze', testcase_id, job_type, queue)

  if testcase.bug_information:
    issue = get_issue_for_testcase(testcase)
    if issue:
      report_url = TESTCASE_REPORT_URL.format(
          domain=get_domain(), testcase_id=testcase_id)
      issue.dirty = True
      issue.comment = ('ClusterFuzz is analyzing your testcase. '
                       'Developers can follow the progress at %s.' % report_url)
      issue.save()

  return testcase.key.id()


# ------------------------------------------------------------------------------
# TestcaseGroup related functions
# ------------------------------------------------------------------------------


def delete_group(group_id, update_testcases=True):
  """Delete the testcase group with the specified id if it exists."""
  # Remove all testcases from the group.
  if update_testcases:
    testcases = get_testcases_in_group(group_id)
    for testcase in testcases:
      remove_testcase_from_group(testcase)

  # Delete the group itself.
  group = get_entity_by_type_and_id(data_types.TestcaseGroup, group_id)
  if group:
    group.key.delete()


def get_testcase_ids_in_group(group_id):
  """Return the all testcase ids in the specified group."""
  if not group_id or not str(group_id).isdigit():
    return []

  query = ndb_utils.get_all_from_query(
      data_types.Testcase.query(data_types.Testcase.group_id == int(group_id)),
      keys_only=True)
  return [key.id() for key in query]


def get_testcases_in_group(group_id):
  """Return the all testcases in the specified group."""
  # Fetch by keys (strongly consistent) to avoid stale results from query
  # (eventually consistent).
  testcases = []
  for testcase_id in get_testcase_ids_in_group(group_id):
    try:
      testcases.append(get_testcase_by_id(testcase_id))
    except errors.InvalidTestcaseError:
      # Already deleted.
      continue

  return testcases


def remove_testcase_from_group(testcase):
  """Removes a testcase from group."""
  if not testcase:
    return

  testcase.group_id = 0
  testcase.group_bug_information = 0
  testcase.put()


def update_group_bug(group_id):
  """Update group bug information for a group."""
  if not group_id:
    # No associated group, no work to do. Bail out.
    return

  testcases = get_testcases_in_group(group_id)
  if not testcases:
    # No group members found. Bail out.
    return

  group_bug_information = 0
  for testcase in testcases:
    if not testcase.bug_information:
      continue

    issue_id = int(testcase.bug_information)
    if not group_bug_information:
      group_bug_information = issue_id
    else:
      group_bug_information = min(group_bug_information, issue_id)

  for testcase in testcases:
    testcase.group_bug_information = group_bug_information
  ndb.put_multi(testcases)


# ------------------------------------------------------------------------------
# Generic helper functions for any data type
# ------------------------------------------------------------------------------


@retry.wrap(
    retries=DEFAULT_FAIL_RETRIES,
    delay=DEFAULT_FAIL_WAIT,
    function='datastore.data_handler.get_entity_by_type_and_id')
def get_entity_by_type_and_id(entity_type, entity_id):
  """Return the datastore object with the given type and id if it exists."""
  if not entity_id or not str(entity_id).isdigit() or int(entity_id) == 0:
    return None

  return entity_type.get_by_id(int(entity_id))


# ------------------------------------------------------------------------------
# Fuzz target related functions
# ------------------------------------------------------------------------------


def get_fuzz_target(name):
  """Get FuzzTarget by fully qualified name."""
  return ndb.Key(data_types.FuzzTarget, name).get()


def get_fuzz_target_job(fuzz_target_name, job):
  """Get FuzzTargetJob by fully qualified name and job."""
  return ndb.Key(data_types.FuzzTargetJob,
                 data_types.fuzz_target_job_key(fuzz_target_name, job)).get()


def get_fuzz_targets(engine=None, project=None, binary=None):
  """Return a Datastore query for fuzz targets."""
  query = data_types.FuzzTarget().query()

  if engine:
    query = query.filter(data_types.FuzzTarget.engine == engine)

  if project:
    query = query.filter(data_types.FuzzTarget.project == project)

  if binary:
    query = query.filter(data_types.FuzzTarget.binary == binary)

  return ndb_utils.get_all_from_query(query)


def get_fuzzing_engines():
  """Return the fuzzing engines currently running."""
  query = data_types.FuzzTarget.query(
      projection=[data_types.FuzzTarget.engine], distinct=True)
  return [f.engine for f in ndb_utils.get_all_from_query(query)]


def is_fuzzing_engine(name):
  """Return whether or not |name| is a fuzzing engine."""
  query = data_types.FuzzTarget.query(data_types.FuzzTarget.engine == name)
  return bool(query.count(limit=1))


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def get_all_fuzzer_names_including_children(include_parents=False):
  """Returns all fuzzer names, including expanded child fuzzers."""
  all_fuzzers = set()
  engine_fuzzers = get_fuzzing_engines()

  fuzzers = data_types.Fuzzer.query(projection=['name'])
  for fuzzer in fuzzers:
    # Add this if we're including all parents or this is not an engine fuzzer
    # with fuzz targets.
    if include_parents or fuzzer.name not in engine_fuzzers:
      all_fuzzers.add(fuzzer.name)

  for fuzz_target in get_fuzz_targets():
    all_fuzzers.add(fuzz_target.fully_qualified_name())

  return sorted(list(all_fuzzers))


@memoize.wrap(memoize.Memcache(MEMCACHE_TTL_IN_SECONDS))
def get_all_job_type_names():
  """Return all job type names."""
  query = data_types.Job.query(projection=['name'])
  return sorted([_.name for _ in query])


def get_coverage_information(fuzzer_name, date, create_if_needed=False):
  """Get coverage information, or create if it doesn't exist."""
  coverage_info = ndb.Key(
      data_types.CoverageInformation,
      data_types.coverage_information_key(fuzzer_name, date)).get()

  if not coverage_info and create_if_needed:
    coverage_info = data_types.CoverageInformation(
        fuzzer=fuzzer_name, date=date)

  return coverage_info