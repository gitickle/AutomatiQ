import queue

import pytest

from automatiq.core import events
from automatiq.core.cancel_standard import CancelToken
from automatiq.core.main import run_agent
from automatiq.core.schema import AssistantResponse, Message, PythonScript, ToolEnum


@pytest.fixture
def mock_config_workspace(tmp_path, mocker):
    mocker.patch("automatiq.core.main.config.WORKSPACE_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def session_dump_dir(mock_config_workspace):
    dump_dir = mock_config_workspace / "session_dump"
    dump_dir.mkdir()
    (dump_dir / "dummy.txt").write_text("dummy")
    return dump_dir


@pytest.fixture
def mock_sandbox(mocker):
    sandbox_cls = mocker.patch("automatiq.core.main.AgentSandbox")
    instance = sandbox_cls.return_value
    instance.execute.return_value = "Mocked execution output"
    instance.cancel_result = None
    instance._cancel_result = None
    return instance


@pytest.fixture
def mock_instructor_client(mocker):
    client_mock = mocker.patch("automatiq.core.main.instructor.from_litellm")
    mock_instance = mocker.MagicMock()
    client_mock.return_value = mock_instance
    return mock_instance


def test_agent_startup_and_missing_session(mock_config_workspace, mocker):
    """Verify that the agent exits if the session_dump directory is missing."""
    log_error_mock = mocker.patch.object(events.log_error, "send")

    with pytest.raises(SystemExit) as exc_info:
        run_agent()

    assert exc_info.value.code == 1
    log_error_mock.assert_called_once()
    assert "No recorded session found" in log_error_mock.call_args[1]["text"]


def test_agent_user_exit(session_dump_dir, mock_sandbox, mock_instructor_client, mocker):
    """Verify that providing 'q' cleanly exits the agent."""
    log_info_mock = mocker.patch.object(events.log_info, "send")

    input_q = queue.Queue()
    input_q.put("q")

    run_agent(input_queue=input_q)

    # Should break and log exit
    log_info_mock.assert_any_call("core", text="User requested exit.")


def test_agent_cancellation_during_llm(session_dump_dir, mock_sandbox, mock_instructor_client, mocker):
    """Verify that a cancel token interrupting the LLM cleanly returns to prompt."""
    operation_cancelled_mock = mocker.patch.object(events.operation_cancelled, "send")

    input_q = queue.Queue()
    input_q.put("")  # first prompt proceeds to llm call
    input_q.put("q")  # second prompt exits after cancellation

    cancel_token = CancelToken()

    def mock_llm_call(*args, **kwargs):
        cancel_token.cancel()
        from automatiq.core.cancel_standard import CancelRequestedException

        raise CancelRequestedException("Interrupted by mock cancel token")

    mock_instructor_client.chat.completions.create_with_completion.side_effect = mock_llm_call

    run_agent(input_queue=input_q, cancel_token=cancel_token)

    operation_cancelled_mock.assert_called_once_with("core")


def test_agent_tool_dispatch_message(session_dump_dir, mock_sandbox, mock_instructor_client, mocker):
    """Verify that the message_to_user tool correctly triggers UI events."""
    tool_message_mock = mocker.patch.object(events.tool_message, "send")

    input_q = queue.Queue()
    input_q.put("")
    input_q.put("q")

    mock_raw_response = mocker.MagicMock()
    mock_raw_response.usage.prompt_tokens = 100

    mock_response = AssistantResponse(
        thought_process="I will tell the user something because I have finished investigating.",
        tool=ToolEnum.message_to_user,
        tool_content=Message(message_to_user="Hello user", does_it_contain_the_final_script=False),
    )

    mock_instructor_client.chat.completions.create_with_completion.return_value = (mock_response, mock_raw_response)

    run_agent(input_queue=input_q)

    tool_message_mock.assert_called_once()
    assert "Hello user" in tool_message_mock.call_args[1]["text"]


def test_agent_tool_dispatch_execute(session_dump_dir, mock_sandbox, mock_instructor_client, mocker):
    """Verify that execute_ipython correctly interacts with the sandbox and triggers UI events."""
    code_exec_start_mock = mocker.patch.object(events.code_exec_start, "send")
    code_exec_output_mock = mocker.patch.object(events.code_exec_output, "send")

    input_q = queue.Queue()
    input_q.put("")
    input_q.put("q")

    mock_raw_response = mocker.MagicMock()
    mock_raw_response.usage.prompt_tokens = 100

    resp_exec = AssistantResponse(
        thought_process="I will run some code to check the current state of the document.",
        tool=ToolEnum.execute_ipython,
        tool_content=PythonScript(ipython_script="print('hi')"),
    )
    resp_msg = AssistantResponse(
        thought_process="I am done with the execution and will now talk to the user.",
        tool=ToolEnum.message_to_user,
        tool_content=Message(message_to_user="Done", does_it_contain_the_final_script=False),
    )

    mock_instructor_client.chat.completions.create_with_completion.side_effect = [
        (resp_exec, mock_raw_response),
        (resp_msg, mock_raw_response),
    ]

    run_agent(input_queue=input_q)

    # Sandbox execute should be called with the script
    mock_sandbox.execute.assert_called_once_with("print('hi')")

    # Code execution events should have fired
    code_exec_start_mock.assert_called()
    code_exec_output_mock.assert_called_once()
    assert code_exec_output_mock.call_args[1]["output"] == "Mocked execution output"
