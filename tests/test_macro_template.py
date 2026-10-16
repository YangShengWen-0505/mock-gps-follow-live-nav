"""Exercise exported payload expressions; this does not emulate Android."""

import json
import re
from pathlib import Path

import pytest

from mock_gps.api.routes import _validate_mission_payload


def controller():
    export = json.loads((Path(__file__).parents[1] / "macrodroid-example.category").read_text(encoding="utf-8"))
    return next(m for m in export["macros"] if m["m_name"].startswith("Mission Controller"))


def expand(text, values):
    return re.sub(
        r"\{(lv|lvjson)=([^}]+)\}",
        lambda m: json.dumps(values[m[2]], ensure_ascii=False) if m[1] == "lvjson" else str(values[m[2]]),
        text,
    )


@pytest.mark.parametrize("count", [1, 10, 50])
def test_exported_payload_preserves_all_stops_and_types(count):
    macro = controller()
    actions = macro["m_actionList"]
    setters = [a for a in actions if a["m_classType"] == "SetVariableAction"]
    body = [a for a in setters if a["m_variable"]["m_name"] == "JSON_Body"]
    fields = [a for a in setters if a["m_variable"]["m_name"] == "StopData"]
    separator = next(a for a in setters if a["m_variable"]["m_name"] == "StopSeparator")
    clear = actions[0]["variableNames"]
    assert {"JSON_Body", "StopData", "StopSeparator"} <= set(clear)
    assert len(body) == 3
    values = {"InitLoc": "25,121", "StopSeparator": "", "StopData": {}}
    values["JSON_Body"] = expand(body[0]["m_newStringValue"], values)
    expected = []
    for index in range(count):
        name = f'Stop {index}: "quoted" \\ path\n新站 {{lv=TempName}}'
        skip = index % 2 == 0
        values.update(TempName=name, TempCoord="", TempMode="walking", TempType="", TempWait="")
        for action in fields:
            keys = action["dictionaryKeys"]
            assert len(keys) == 1, "No unverified nested dictionary creation"
            kind = action["dictionaryOrArrayType"]
            assert kind in (0, 2), "SetVariable must explicitly select the leaf value type"
            if kind == 0:
                if action["m_newBooleanValue"] and not skip:
                    continue  # Corresponding TempSkip == true IfConditionAction.
                value = action["m_newBooleanValue"]
            else:
                value = expand(action["m_newStringValue"], values)
            values["StopData"][keys[0]] = value
        values["JSON_Body"] = expand(body[1]["m_newStringValue"], values)
        values["StopSeparator"] = separator["m_newStringValue"]
        expected.append(dict(name=name, coord="", mode="walking", transit_type="", wait_time="", skip_if_late=skip))
    payload = json.loads(expand(body[2]["m_newStringValue"], values))
    assert payload == {"init_loc": "25,121", "stops": expected}
    assert _validate_mission_payload(payload)[1] == expected


def test_serialization_occurs_inside_loop_before_counter_increment():
    actions = controller()["m_actionList"]
    append = next(i for i, a in enumerate(actions) if "{lvjson=StopData}" in a.get("m_newStringValue", ""))
    start = next(i for i, a in enumerate(actions) if a["m_classType"] == "LoopAction")
    end = next(i for i, a in enumerate(actions) if a["m_classType"] == "EndLoopAction")
    assert start < append < end
    assert actions[append + 2]["m_variable"]["m_name"] == "CurrentLoop"
    http = next(a for a in actions if a["m_classType"] == "HttpRequestAction")
    assert http["requestConfig"]["contentBodyText"] == "{lv=JSON_Body}"


def test_server_configuration_survives_export_and_runtime_reset():
    macro = controller()
    values = {v["m_name"]: v["m_stringValue"] for v in macro["localVariables"]}
    assert values["g_server_url"].startswith(("http://", "https://"))
    for action in macro["m_actionList"]:
        if action["m_classType"] == "ClearVariablesAction":
            assert "g_server_url" not in action["variableNames"]
    http = next(a["requestConfig"] for a in macro["m_actionList"] if a["m_classType"] == "HttpRequestAction")
    url = expand(http["urlToOpen"], values)
    assert url.startswith(values["g_server_url"])
    assert url.endswith("/start_task")
    assert http["blockNextAction"] is True
