"""Main entrypoint for WDL2CWL."""

import argparse
import re
import sys
import textwrap
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Type, TypeVar, Union, cast

import cwl_utils.parser.cwl_v1_2 as cwl
import regex  # type: ignore
import WDL
import WDL._parser  # delete when reloading bug is fixed upstream
import WDL.CLI
from ruamel.yaml import scalarstring
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.main import YAML

from wdl2cwl import _logger
from wdl2cwl.errors import WDLSourceLine

valid_js_identifier = regex.compile(
    r"^(?!(?:do|if|in|for|let|new|try|var|case|else|enum|eval|null|this|true|"
    r"void|with|break|catch|class|const|false|super|throw|while|yield|delete|export|"
    r"import|public|return|static|switch|typeof|default|extends|finally|package|"
    r"private|continue|debugger|function|arguments|interface|protected|implements|"
    r"instanceof)$)(?:[$_\p{ID_Start}])(?:[$_\u200C\u200D\p{ID_Continue}])*$"
)

# ^^ is a combination of https://github.com/tc39/proposal-regexp-unicode-property-escapes#other-examples
# and regex at the bottom of https://stackoverflow.com/a/9392578
# double checked against https://262.ecma-international.org/5.1/#sec-7.6
# eval is not on the official list of reserved words, but it is a built-in function


class ConversionException(Exception):
    """Error during conversion."""


def convert(doc: str) -> dict[str, Any]:
    """Convert a WDL workflow, reading the file, into a CWL workflow Python object."""
    WDL._parser._lark_comments_buffer.clear()
    root_folder = str(Path(doc).parent)
    try:
        doc_tree = WDL.load(
            doc, [root_folder], read_source=WDL.CLI.make_read_source(False), check_quant=True  # type: ignore[no-untyped-call]
        )
    except (
        WDL.Error.SyntaxError,
        WDL.Error.ImportError,
        WDL.Error.ValidationError,
        WDL.Error.MultipleValidationErrors,
    ) as exn:
        WDL.CLI.print_error(exn)
        raise exn

    parser = Converter()
    if doc_tree.workflow:
        return sort_cwl(parser.load_wdl_workflow(doc_tree.workflow).save())
    if len(doc_tree.tasks) == 1:
        return sort_cwl(parser.load_wdl_objects(doc_tree.tasks[0]).save())
    else:
        return {
            "cwlVersion": "v1.2",
            "$graph": [
                sort_cwl(parser.load_wdl_objects(task).save())
                for task in doc_tree.tasks
            ],
        }


def sort_cwl(document: dict[str, Any]) -> CommentedMap:
    """Sort the sections of the CWL document in a more meaningful order."""
    keyorder = [
        "cwlVersion",
        "id",
        "class",
        "label",
        "doc",
        "requirements",
        "hints",
        "inputs",
        "stdin",
        "baseCommand",
        "steps",
        "expression",
        "arguments",
        "stderr",
        "stdout",
        "outputs",
        "successCodes",
        "temporaryFailCodes",
        "permanentFailCodes",
    ]
    return CommentedMap(
        sorted(
            document.items(),
            key=lambda i: keyorder.index(i[0]) if i[0] in keyorder else 100,
        )
    )


CWLArrayTypes = TypeVar(
    "CWLArrayTypes",
    cwl.InputArraySchema,
    cwl.OutputArraySchema,
    cwl.CommandInputArraySchema,
    cwl.CommandOutputArraySchema,
)
CWLRecordTypes = TypeVar(
    "CWLRecordTypes",
    cwl.InputRecordSchema,
    cwl.OutputRecordSchema,
    cwl.CommandInputRecordSchema,
    cwl.CommandOutputRecordSchema,
)


def get_mem_in_bytes(unit: str) -> tuple[int, int]:
    """
    Determine the value of a memory unit in bytes.

    Returns the base and exponent, ready for stringifying or evaluation
    """
    if unit == "KiB" or unit == "Ki":
        return 1024, 1
    elif unit == "MiB" or unit == "Mi":
        return 1024, 2
    elif unit == "GiB" or unit == "Gi":
        return 1024, 3
    elif unit == "TiB" or unit == "Ti":
        return 1024, 4
    elif unit == "B":
        return 1024, 0
    elif unit == "KB" or unit == "K":
        return 1000, 1
    elif unit == "MB" or unit == "M":
        return 1000, 2
    elif unit == "GB" or unit == "G":
        return 1000, 3
    elif unit == "TB" or unit == "T":
        return 1000, 4
    raise ConversionException(f"Invalid memory unit: ${unit}")


def get_input(input_name: str) -> str:
    """Produce a concise, valid CWL expr/param reference lookup string for a given input name."""
    if valid_js_identifier.match(input_name):
        return f"inputs.{input_name}"
    return f'inputs["{input_name}"]'


def get_cwl_docker_requirements(
    wdl_docker: Union[WDL.Expr.Get, WDL.Expr.String],
) -> Optional[cwl.ProcessRequirement]:
    """Translate WDL Runtime Docker requirements to CWL Docker Requirement."""
    if isinstance(wdl_docker, WDL.Expr.String) and wdl_docker.literal:
        dockerpull = get_literal_value(wdl_docker)
    else:
        if isinstance(wdl_docker, WDL.Expr.String):
            parts = wdl_docker.parts
            docker_placeholder = [
                pl_holder
                for pl_holder in parts
                if isinstance(pl_holder, WDL.Expr.Placeholder)
            ]
            dockerpull_expr = docker_placeholder[0].expr.expr  # type: ignore[attr-defined]
        else:
            dockerpull_expr = wdl_docker.expr
        if dockerpull_expr is None or not isinstance(dockerpull_expr, WDL.Expr.Ident):
            raise WDLSourceLine(wdl_docker, ConversionException).makeError(
                f"Unsupported type: {type(dockerpull_expr)}: {dockerpull_expr}"
            )
        dockerpull_referee = dockerpull_expr.referee
        if dockerpull_referee is None:
            raise WDLSourceLine(wdl_docker, ConversionException).makeError(
                f"Unsupported type: {type(dockerpull_referee)}"
            )
        if dockerpull_referee.expr:
            dockerpull = get_literal_value(dockerpull_referee.expr)
        else:
            _logger.warning(f"Unable to extract docker reference from {wdl_docker}")
            return None
    return cwl.DockerRequirement(dockerPull=dockerpull)


def get_expr_name(wdl_expr: WDL.Expr.Ident) -> str:
    """Extract name from WDL expr."""
    return get_input(wdl_expr.name)


def get_expr_name_with_is_file_check(wdl_expr: WDL.Expr.Ident) -> str:
    """Extract name from WDL expr and check if it's a file path."""
    expr_name = get_input(wdl_expr.name)
    is_file = isinstance(wdl_expr.type, WDL.Type.File)
    return expr_name if not is_file else f"{expr_name}.path"


def get_literal_value(expr: WDL.Expr.Base) -> Optional[Any]:
    """Recursively get a literal value."""
    if literal := expr.literal:
        if (
            hasattr(expr.parent, "type")
            and expr.parent is not None
            and isinstance(expr.parent.type, WDL.Type.File)
        ):
            return {"class": "File", "path": literal.value}
        value = literal.value
        if isinstance(expr.type, WDL.Type.Map):
            return {key.value: val.value for key, val in value}
        if isinstance(value, list):
            result = []
            for item in value:
                if (
                    hasattr(expr.parent, "type")
                    and expr.parent is not None
                    and isinstance(expr.parent.type.item_type, WDL.Type.File)
                ):
                    result.append({"class": "File", "path": item.value})
                else:
                    result.append(item.value)
            return result
        return value
    return None


def nice_quote(value: str) -> str:
    """Surround string with quotes, with minimal escaping."""
    single = "'" in value
    double = '"' in value
    if not double:
        return f'"{value}"'
    if not single and double:
        return f"'{value}'"
    # single and double quotes found
    return '"' + value.replace('"', r"\"") + '"'


read_funcs = {
    "read_string": r"$(self[0].contents.replace(/[\r\n]+$/, ''))",
    "read_int": "$(parseInt(self[0].contents))",
    "read_float": "$(parseFloat(self[0].contents))",
    "read_boolean": """${
  var contents = self[0].contents.trim().toLowerCase()
  if (contents == 'true') { return true;}
  if (contents == 'false') { return false;}
  throw "'read_boolean' received neither 'true' nor 'false': " + self[0].contents;
}""",
    "read_lines": r"""${
  var contents = self[0].contents.replace(/\r\n$/, "").replace(/\n$/, "").replace(/\r$/, "");
  // ^ remove any trailing newline to prevent a null being returned
  return contents.split(/\r\n|\r|\n/);
}""",
    "read_tsv": r"""${
  var result = Array();
  var contents = self[0].contents.replace(/\r\n$/, "").replace(/\n$/, "").replace(/\r$/, "");
  // ^ remove any trailing newline to prevent a null being returned
  contents.split(/\r\n|\r|\n/).forEach(function(line) {
    result.push(line.split('\t'));
  });
  return result;
}""",
}


class Converter:
    """Object that handles WDL Workflows and task conversion to CWL."""

    def __init__(self) -> None:
        """Initialize the sets used by the object and prevent inconsistent behaviours."""
        self.non_static_values: set[str] = set()
        self.optional_cwl_null: set[str] = set()
        self.scatter_names: list[str] = []

    def load_wdl_objects(
        self, obj: Union[WDL.Tree.Task, WDL.Tree.Workflow]
    ) -> Union[cwl.CommandLineTool, cwl.Workflow]:
        """Load a WDL SourceNode obj and returns either a Task or a Workflow."""
        if isinstance(obj, WDL.Tree.Task):
            return self.load_wdl_task(obj)
        elif isinstance(obj, WDL.Tree.Workflow):
            return self.load_wdl_workflow(obj)

    def get_step_input_expr(
        self, wf_expr: Union[WDL.Expr.Get, WDL.Expr.String]
    ) -> tuple[str, Optional[str]]:
        """
        Get name of expression referenced in workflow call inputs.

        :return: The source plus any needed "valueFrom" expression.
        """
        with WDLSourceLine(wf_expr, ConversionException):
            if isinstance(wf_expr, WDL.Expr.String):
                return self.get_expr_string(wf_expr, False)[0][1:-1], None
            elif isinstance(wf_expr, WDL.Expr.Get):
                if isinstance(wf_expr.expr, WDL.Expr.Ident):
                    member = None
                    id_name = wf_expr.expr.name
                    referee = wf_expr.expr.referee
                    if (
                        referee
                        and isinstance(referee, WDL.Tree.Scatter)
                        and isinstance(referee.expr, (WDL.Expr.Get, WDL.Expr.String))
                    ):
                        scatter_name, value_from = self.get_step_input_expr(
                            referee.expr
                        )
                        self.scatter_names.append(scatter_name)
                        return scatter_name, value_from
                    return id_name, None
                elif isinstance(wf_expr.expr, WDL.Expr.Get):
                    member = str(wf_expr.member)
                    ident = cast(WDL.Expr.Ident, wf_expr.expr.expr)
                    id_name = ident.name
            elif isinstance(wf_expr, WDL.Expr.Apply):
                expr_str, _, sources = self.get_expr(wf_expr)
                if len(sources) == 1:
                    id_name = sources[0]
                    value_from = "self" + expr_str.partition(f"inputs.{id_name}")[2]
                    return id_name, value_from
            else:
                return get_literal_value(wf_expr), None
            return id_name, f"self.{member}" if member else None

    def load_wdl_workflow(self, obj: WDL.Tree.Workflow) -> cwl.Workflow:
        """Load WDL workflow and convert to CWL."""
        wf_name = obj.name
        requirements: list[cwl.ProcessRequirement] = [cwl.InlineJavascriptRequirement()]
        inputs = self.get_cwl_workflow_inputs(obj.available_inputs, obj.parameter_meta)
        wf_steps: list[cwl.WorkflowStep] = []
        outputs: list[cwl.WorkflowOutputParameter] = []
        scatter_present = False
        step_valuefrom = False
        for (
            output_id,
            output_type,
            output_source,
            extra_step,
            value_from,
        ) in self.get_workflow_outputs(obj.effective_outputs):
            outputs.append(
                cwl.WorkflowOutputParameter(
                    id=f"{wf_name}.{output_id}",
                    type_=output_type,
                    outputSource=output_source,
                    doc=obj.meta.get(output_id, None),
                )
            )
            if extra_step:
                wf_steps.append(extra_step)
            if value_from:
                step_valuefrom = True
        wf_description = (
            obj.meta.pop("description") if "description" in obj.meta else None
        )
        for body_part in obj.body:
            with WDLSourceLine(body_part, ConversionException):
                if (
                    isinstance(body_part, WDL.Tree.Decl)
                    and body_part.expr is not None
                    and get_literal_value(body_part.expr) is not None
                ):
                    extra = WDL.Env.Bindings(
                        binding=WDL.Env.Binding(name=body_part.name, value=body_part)
                    )
                    inputs.extend(
                        self.get_cwl_workflow_inputs(extra, obj.parameter_meta)
                    )
                elif isinstance(body_part, WDL.Tree.Conditional):
                    if len(body_part.body) > 1:
                        raise ConversionException(
                            "Multi-task conditionals are not yet supported. Please open an issue with an example!"
                        )
                    step = self.get_workflow_call(body_part.body[0])  # type: ignore
                    if isinstance(body_part.expr, (WDL.Expr.Apply, WDL.Expr.Get)):
                        when, _, sources = self.get_expr(body_part.expr, None, True)
                        step.when = f"$({when})"
                        wf_steps.append(step)
                    else:
                        raise ConversionException(
                            "Conditional expression type: "
                            f"{type(body_part.expr)} is not yet handled. "
                            "Please open an issue with an example."
                        )
                elif isinstance(body_part, WDL.Tree.Call):
                    step = self.get_workflow_call(body_part)
                    wf_steps.append(step)
                    for inp in step.in_:
                        if inp.valueFrom is not None:
                            step_valuefrom = True
                elif isinstance(body_part, WDL.Tree.Scatter):
                    scatter_present = True
                    wf_steps.extend(self.get_workflow_scatter(body_part))
                else:
                    _logger.warning(
                        WDLSourceLine(body_part).makeError(
                            "Warning: unhandled Workflow node type:"
                        )
                        + " %s",
                        type(body_part),
                    )
                continue
        if scatter_present:
            requirements.append(cwl.ScatterFeatureRequirement())
        if step_valuefrom:
            requirements.append(cwl.StepInputExpressionRequirement())

        return cwl.Workflow(
            id=wf_name,
            cwlVersion="v1.2",
            doc=wf_description,
            requirements=requirements if requirements else None,
            inputs=inputs,
            steps=wf_steps,
            outputs=outputs,
        )

    def get_workflow_scatter(self, scatter: WDL.Tree.Scatter) -> list[cwl.WorkflowStep]:
        """Get the CWL Workflow Step equivalent of a list of WDL Scatter Object."""
        scatter_steps: list[cwl.WorkflowStep] = []
        for body_obj in scatter.body:
            with WDLSourceLine(body_obj, ConversionException):
                if isinstance(body_obj, WDL.Tree.Call):
                    wf_step = self.get_workflow_call(body_obj)
                wf_step.scatter = self.scatter_names.pop()
                scatter_steps.append(wf_step)
        return scatter_steps

    def get_workflow_call(self, call: WDL.Tree.Call) -> cwl.WorkflowStep:
        """Get the CWL Workflow Step equivalent of a WDL Call Object."""
        callee = call.callee
        if callee is None:
            raise ConversionException("callee is None. This should not be possible.")
        cwl_callee_inputs = self.get_cwl_task_inputs(callee.inputs)[0]
        inputs_from_call: dict[str, tuple[str, dict[str, Any]]] = {}
        input_defaults = set()
        if call_inputs := call.inputs:
            scatter_handled = False
            for key, value in call_inputs.items():
                with WDLSourceLine(value, ConversionException):
                    if not isinstance(value, (WDL.Expr.Get, WDL.Expr.Apply)):
                        input_defaults.add(key)
                    if (
                        isinstance(value, WDL.Expr.Apply)
                        and value.function_name == "select_all"
                        and isinstance(value.arguments[0], WDL.Expr.Array)
                        and isinstance(
                            value.arguments[0].items[0],
                            (WDL.Expr.Get, WDL.Expr.String),
                        )
                    ):
                        input_expr, value_from = self.get_step_input_expr(
                            value.arguments[0].items[0]
                        )
                        value_str = f"$([{value_from if value_from else 'self'}])"
                        inputs_from_call[key] = (
                            input_expr.replace(".", "/"),
                            {
                                "pickValue": "all_non_null",
                                "valueFrom": value_str,
                            },
                        )
                    else:
                        input_expr, value_from = self.get_step_input_expr(value)  # type: ignore[arg-type]
                        inputs_from_call[key] = (
                            (
                                input_expr.replace(".", "/")
                                if isinstance(input_expr, str)
                                else input_expr
                            ),
                            {"valueFrom": f"$({value_from})"} if value_from else {},
                        )
                    if self.scatter_names and not scatter_handled:
                        self.scatter_names[-1] = key
                        scatter_handled = True
        wf_step_inputs: list[cwl.WorkflowStepInput] = []
        for inp in cwl_callee_inputs:
            call_inp_id = f"{call.name}.{inp.id}"
            source_str, extras = inputs_from_call.get(
                cast(str, inp.id), (call_inp_id, {})
            )

            if inp.id not in input_defaults:
                wf_step_inputs.append(
                    cwl.WorkflowStepInput(id=inp.id, source=source_str, **extras)
                )
            else:
                wf_step_inputs.append(
                    cwl.WorkflowStepInput(id=inp.id, default=source_str, **extras)
                )
        wf_step_outputs = (
            [cwl.WorkflowStepOutput(id=output.name) for output in callee.outputs]
            if callee.outputs
            else []
        )
        wf_step_run = self.load_wdl_objects(callee)
        wf_step = cwl.WorkflowStep(
            wf_step_inputs,
            id=call.name,
            run=wf_step_run,
            out=wf_step_outputs,
        )
        return wf_step

    def load_wdl_task(self, obj: WDL.Tree.Task) -> cwl.CommandLineTool:
        """Load task and convert to CWL."""
        description = obj.meta.pop("description", None)
        cwl_inputs, restrictions = self.get_cwl_task_inputs(
            obj.inputs, obj.parameter_meta
        )
        run_script = False
        hints, requirements = self.get_cwl_hints_and_requirements(obj)
        for req in requirements:
            if isinstance(req, cwl.InitialWorkDirRequirement):
                run_script = True
        arguments = [{"valueFrom": "${" + item + "}"} for item in restrictions]
        tool = cwl.CommandLineTool(
            id=obj.name,
            doc=description,
            inputs=cwl_inputs,
            outputs=None,
            hints=hints,
            requirements=requirements,
            cwlVersion="v1.2",
            baseCommand=["bash", "script.bash"] if run_script else ["true"],
            arguments=arguments if arguments else None,
        )
        tool.outputs = self.set_cwl_task_outputs(obj.outputs, obj.parameter_meta, tool)
        if obj.meta:
            _logger.warning("Skipping meta: %s", obj.meta)
        return tool

    def get_cwl_hints_and_requirements(
        self, obj: WDL.Tree.Task
    ) -> tuple[list[cwl.ProcessRequirement], list[cwl.ProcessRequirement]]:
        """Produce the CWL Requirements list."""
        command = obj.command
        runtime = obj.runtime
        requirements: list[cwl.ProcessRequirement] = []
        hints: list[cwl.ProcessRequirement] = []
        if "docker" in runtime:
            with WDLSourceLine(runtime["docker"], ConversionException):
                docker = get_cwl_docker_requirements(
                    runtime["docker"]  # type: ignore[arg-type]
                )
                if docker:
                    hints.append(docker)
        if command_req := self.get_cwl_command_requirements(command.parts):
            requirements.append(command_req)
        requirements.append(cwl.InlineJavascriptRequirement())
        requirements.append(cwl.NetworkAccess(networkAccess=True))
        cpu_requirement = (
            self.get_cpu_requirement(runtime["cpu"]) if "cpu" in runtime else None
        )
        if "memory" in runtime:
            with WDLSourceLine(runtime["memory"], ConversionException):
                memory_requirement = self.get_memory_requirement(
                    runtime["memory"]  # type: ignore[arg-type]
                )
        else:
            memory_requirement = None
        if "disks" in runtime:
            with WDLSourceLine(runtime["disks"], ConversionException):
                outdir_requirement = self.get_outdir_requirement(
                    runtime["disks"]  # type: ignore[arg-type]
                )
        else:
            outdir_requirement = 1024
        hints.append(
            cwl.ResourceRequirement(
                coresMin=cpu_requirement,
                ramMin=memory_requirement,
                outdirMin=outdir_requirement,
            )
        )
        if "time_minutes" in runtime:
            with WDLSourceLine(runtime["time_minutes"], ConversionException):
                time_minutes = self.get_time_minutes_requirement(
                    runtime["time_minutes"]  # type: ignore[arg-type]
                )
            hints.append(
                cwl.ToolTimeLimit(
                    timelimit=time_minutes,
                )
            )
        return hints, requirements

    def get_time_minutes_requirement(
        self, time_minutes: WDL.Expr.Get
    ) -> Union[str, int]:
        """Produce the time limit expression from WDL runtime time minutes."""
        with WDLSourceLine(time_minutes, ConversionException):
            if isinstance(time_minutes, (WDL.Expr.Int, WDL.Expr.Float)):
                return cast(int, get_literal_value(time_minutes)) * 60
            time_minutes_str, _, _ = self.get_expr(time_minutes)
        return f"$({time_minutes_str} * 60)"

    def get_memory_requirement(
        self, memory_runtime: Union[WDL.Expr.Get, WDL.Expr.String]
    ) -> Union[str, float]:
        """Translate WDL Runtime Memory requirement to CWL Resource Requirement."""
        with WDLSourceLine(memory_runtime, ConversionException):
            if isinstance(memory_runtime, WDL.Expr.String):
                ram_min_literal = self.get_memory_literal(memory_runtime)
                return ram_min_literal
            elif isinstance(memory_runtime, WDL.Expr.Apply):
                expr, unit = memory_runtime.arguments
                ram_min, _, _ = self.get_expr(expr)
                return self.get_ram_min_js(
                    ram_min, str(get_literal_value(unit)).strip()
                )
            ram_min = get_expr_name(memory_runtime.expr)  # type: ignore[arg-type]
            return self.get_ram_min_js(ram_min, "")

    def get_memory_literal(self, memory_runtime: WDL.Expr.String) -> Union[float, str]:
        """Get the literal value for memory requirement with type WDL.Expr.String."""
        if memory_runtime.literal is None:
            if len(memory_runtime.parts) == 4:
                _, amount, unit, _ = memory_runtime.parts
            else:
                _, amount, _, unit, _ = memory_runtime.parts
            if isinstance(amount, WDL.Expr.Placeholder):
                amount_str, _, _ = self.get_expr(amount)
            else:
                amount_str = amount
            if isinstance(unit, WDL.Expr.Placeholder):
                unit_str, _, _ = self.get_expr(unit)
            else:
                unit_str = unit.strip()
            return self.get_ram_min_js(amount_str, unit_str)

        ram_min = str(get_literal_value(memory_runtime))
        unit_result = re.search(r"[a-zA-Z]+", ram_min)
        if not unit_result:
            raise ConversionException("Missing Memory units, yet still a string?")
        unit = unit_result.group()
        value = float(ram_min.split(unit)[0])
        unit_base, unit_exponent = get_mem_in_bytes(unit)
        return float((value * (unit_base**unit_exponent)) / (2**20))

    def get_outdir_requirement(
        self, outdir: Union[WDL.Expr.Get, WDL.Expr.Apply, WDL.Expr.Placeholder]
    ) -> Union[int, str]:
        """Produce the memory requirement for the output directory from WDL runtime disks."""
        with WDLSourceLine(outdir, ConversionException):
            if (
                isinstance(outdir, (WDL.Expr.Apply, WDL.Expr.String))
                and outdir.literal is None
            ):
                if (
                    isinstance(outdir, WDL.Expr.Apply)
                    and not outdir.function_name == "_add"
                ):
                    # If it contains an apply expr we don't want to process the _add function
                    # that concatenates it to the chars in the string
                    expr_str, _, _ = self.get_expr(outdir)
                    if isinstance(outdir.type, WDL.Type.String):
                        return f"$(parseFloat({expr_str}) * 1024)"
                    else:
                        return f"$(({expr_str}) * 1024)"
                # apply exprs contain arguments and strings contain parts both are lists
                # they could contain expressions that represent the runtime disk
                list_object = (
                    outdir.arguments
                    if hasattr(outdir, "arguments")
                    else outdir.parts  # type: ignore[attr-defined]
                )
                for obj in list_object:
                    if isinstance(
                        obj, (WDL.Expr.Get, WDL.Expr.Apply, WDL.Expr.Placeholder)
                    ):
                        # avoid python strings only WDL expressions are handled.
                        return self.get_outdir_requirement(obj)
            elif isinstance(outdir, (WDL.Expr.Get, WDL.Expr.Placeholder)):
                expr_str, _, _ = self.get_expr(outdir)
                return (
                    f"$(({expr_str}) * 1024)"
                    if not expr_str.isdigit()
                    else int(expr_str) * 1024
                )
            value = re.search(r"[0-9]+", str(get_literal_value(outdir))).group()  # type: ignore[union-attr]

            return int(value) * 1024

    def get_ram_min_js(self, ram_min_ref_name: str, unit: str) -> str:
        """Get memory requirement for user input."""
        append_str: str = ""
        if unit:
            if "inputs." in unit:
                append_str = "${\nvar unit = " + unit + ";"
            else:
                append_str = '${\nvar unit = "' + unit + '";'
        else:
            append_str = (
                "${\nvar unit = " + ram_min_ref_name + '.match(/[a-zA-Z]+/g).join("");'
            )
        js_str = (
            append_str
            + "\nvar value = parseInt(`${"
            + ram_min_ref_name
            + "}`.match(/[0-9]+/g));\n"
            + 'var memory = "";\n'
            + 'if(unit==="KiB") memory = value/1024;\n'
            + 'else if(unit==="MiB") memory = value;\n'
            + 'else if(unit==="GiB") memory = value*1024;\n'
            + 'else if(unit==="TiB") memory = value*1024*1024;\n'
            + 'else if(unit==="B") memory = value/(1024*1024);\n'
            + 'else if(unit==="KB" || unit==="K") memory = (value*1000)/(1024*1024);\n'
            + 'else if(unit==="MB" || unit==="M") memory = (value*(1000*1000))/(1024*1024);\n'
            + 'else if(unit==="GB" || unit==="G") memory = (value*(1000*1000*1000))/(1024*1024);\n'
            + 'else if(unit==="TB" || unit==="T") memory = (value*(1000*1000*1000*1000))/(1024*1024);\n'
            + 'else throw "Unknown units: " + unit;\n'
            + "return parseInt(memory);\n}"
        )

        return js_str

    def get_cwl_type(
        self,
        input_type: WDL.Type.Base,
        array_type: type[CWLArrayTypes],
        record_type: type[CWLRecordTypes],
        parent: Optional[WDL.Error.SourceNode],
    ) -> Union[str, CWLArrayTypes, CWLRecordTypes]:
        """Determine the CWL type for a WDL input declaration."""
        if isinstance(input_type, WDL.Type.Any):
            return "Any"
        if isinstance(input_type, WDL.Type.File):
            return "File"
        elif isinstance(input_type, WDL.Type.String):
            return "string"
        elif isinstance(input_type, WDL.Type.Boolean):
            return "boolean"
        elif isinstance(input_type, WDL.Type.Int):
            return "int"
        elif isinstance(input_type, WDL.Type.Float):
            return "float"
        elif isinstance(input_type, WDL.Type.Array):
            sub_type = self.get_cwl_type(
                input_type.item_type, array_type, record_type, parent
            )
            return array_type(type_="array", items=sub_type)
        elif isinstance(input_type, WDL.Type.StructInstance):
            return record_type(
                type_="record",
                name=input_type.type_name,
                fields=self.get_struct_inputs(input_type.members),
            )
        else:
            target = input_type if input_type.pos is not None else parent
            raise WDLSourceLine(target, ConversionException).makeError(
                f"Input of type {input_type} is not yet handled."
            )

    def get_workflow_outputs(
        self,
        outputs: WDL.Env.Bindings[WDL.Type.Base],
    ) -> Iterator[
        tuple[
            str,
            Union[cwl.OutputArraySchema, cwl.OutputRecordSchema, str],
            str,
            Optional[cwl.WorkflowStep],
            bool,
        ]
    ]:
        """
        Return the name, CWL type, source, and if valueFrom was used.

        For each of a workflow's effective_outputs().
        """
        for item in outputs:
            value_from = False
            with WDLSourceLine(item.info, ConversionException):
                output_name = item.name
                item_expr = item.info.expr
                member = None
                wdl_output = item.info
                type_of = self.get_cwl_type(
                    wdl_output.type,
                    cwl.OutputArraySchema,
                    cwl.OutputRecordSchema,
                    wdl_output,
                )
                if isinstance(item_expr, WDL.Expr.Apply):
                    new_output_name = f"_{output_name}_{str(item_expr.function_name)}"
                    apply_expr, apply_sources = self.get_expr_apply(item_expr, False)
                    extra_step = cwl.WorkflowStep(
                        in_=[
                            cwl.WorkflowStepInput(
                                id="_".join(source.split("/")), source=source
                            )
                            for source in apply_sources
                        ],
                        out=["result"],
                        run=cwl.ExpressionTool(
                            inputs=[
                                cwl.WorkflowInputParameter(
                                    type_="Any", id="_".join(source.split("/"))
                                )
                                for source in apply_sources
                            ],
                            expression='${ return {"result": '
                            + self.get_expr_apply(item_expr, False)[0]
                            + "}; }",
                            outputs=[
                                cwl.ExpressionToolOutputParameter(
                                    type_=type_of, id="result"
                                )
                            ],
                        ),
                        id=new_output_name,
                    )
                    yield (
                        output_name,
                        type_of,
                        f"{new_output_name}/result",
                        extra_step,
                        value_from,
                    )
                    continue
                if isinstance(item_expr.expr, WDL.Expr.Get) and item_expr.member:
                    member = item_expr.member
                    item_expr = item_expr.expr
                output_source = item_expr.expr.name[::-1].replace(".", "/", 1)[::-1]
                # replace just the last occurrence of a period with a slash
                # by first reversing the string and the replace the first occurrence
                # then reversing the result
                if member:
                    value_from = True
                    new_output_name = f"_{item_expr.expr.name}.{member}"
                    extra_step = cwl.WorkflowStep(
                        in_=[
                            cwl.WorkflowStepInput(
                                id="target",
                                source=output_source,
                                valueFrom=f"$(self.{member})",
                            )
                        ],
                        out=[f"{member}"],
                        run=cwl.ExpressionTool(
                            inputs=[
                                cwl.WorkflowInputParameter(type_="Any", id="target")
                            ],
                            expression='${return {"'
                            + str(member)
                            + '": inputs.target}; }',
                            outputs=[
                                cwl.ExpressionToolOutputParameter(
                                    type_=type_of, id=str(member)
                                )
                            ],
                        ),
                        id=new_output_name,
                    )
                    yield (
                        output_name,
                        type_of,
                        f"{new_output_name}/{member}",
                        extra_step,
                        value_from,
                    )
                else:
                    yield (output_name, type_of, output_source, None, value_from)

    def get_expr(
        self,
        wdl_expr: WDL.Expr.Base,
        target_type: Optional[WDL.Type.Base] = None,
        top: bool = False,
    ) -> tuple[str, Optional[WDL.Type.Base], list[str]]:
        """
        Translate WDL Expressions.

        :param wdl_expr: The WDL expression to translate
        :param target_type: The target WDL type
        :param top: True, if the wdl_expr is a standalone (non-embedded)
                    WDL expression.
        :return: A CWL Expression, and the target WDL type if known, and the list of sources used.
        """
        wdl_type: Optional[WDL.Type.Base] = None
        sources: list[str] = []
        if isinstance(wdl_expr, WDL.Expr.Apply):
            result, sources = self.get_expr_apply(wdl_expr, top)
        elif isinstance(wdl_expr, WDL.Expr.Get):
            result, wdl_type, sources = self.get_expr_get(wdl_expr, top)
        elif isinstance(wdl_expr, WDL.Expr.IfThenElse):
            result, sources = self.get_expr_ifthenelse(wdl_expr)
        elif isinstance(wdl_expr, WDL.Expr.Placeholder):
            result, wdl_type, sources = self.translate_wdl_placeholder(wdl_expr, top)
        elif isinstance(wdl_expr, WDL.Expr.String):
            result, sources = self.get_expr_string(wdl_expr, top)
        elif isinstance(wdl_expr, WDL.Expr.Boolean) and wdl_expr.literal:
            result = str(wdl_expr.literal)  # "true" not "True"
            # no sources
        elif (
            isinstance(
                wdl_expr,
                (
                    WDL.Expr.Boolean,
                    WDL.Expr.Int,
                    WDL.Expr.Float,
                ),
            )
            and wdl_expr.literal
        ):
            result = str(wdl_expr.literal.value)
            # no sources
        elif isinstance(wdl_expr, WDL.Expr.Array):
            items = []
            for item in wdl_expr.items:
                expression, _, item_sources = self.get_expr(item)
                items.append(expression)
                sources.extend(item_sources)
            result = "[ " + ", ".join(items) + " ]"
        elif isinstance(wdl_expr, WDL.Expr.Map):
            decls = []
            for map_key, map_value in wdl_expr.items:
                key_expr, _, key_sources = self.get_expr(map_key)
                value_expr, _, value_sources = self.get_expr(map_value)
                decls.append(f"{key_expr}: {value_expr}")
                sources.extend(key_sources)
                sources.extend(value_sources)
            result = "{ " + ", ".join(decls) + " }"
        elif isinstance(wdl_expr, WDL.Expr.Struct) and isinstance(
            target_type, WDL.Type.StructInstance
        ):
            decls = []
            assert target_type.members is not None  # nosec
            for struct_key, member_type in target_type.members.items():
                key_expr, _, key_sources = self.get_expr(
                    wdl_expr.members[struct_key], member_type
                )
                decls.append(f'"{struct_key}": {key_expr}')
                sources.extend(key_sources)
            result = "{ " + ", ".join(decls) + " }"
        else:  # pragma: no cover
            raise WDLSourceLine(wdl_expr, ConversionException).makeError(
                f"The expression '{wdl_expr}' is not handled yet."
            )
        if target_type and isinstance(target_type, WDL.Type.File):
            return (
                '{ "class": "File", "path": runtime.outdir+"/"+' + result + " }",
                wdl_type,
                sources,
            )
        return result, wdl_type, sources

    def get_expr_string(
        self, wdl_expr_string: WDL.Expr.String, top: bool
    ) -> tuple[str, list[str]]:
        """
        Translate WDL String Expressions.

        :return: The CWL Expression and a list of sources
        """
        if wdl_expr_string.literal is not None:
            return str(wdl_expr_string.literal), []
        parts = wdl_expr_string.parts
        q = cast(str, parts[0])[0]
        sources: list[str] = []
        if isinstance(parts[1], str):
            string = f"{q}{parts[1]}{q}"
        else:
            string, _, sources = self.get_expr(parts[1], None, top)
        if parts[2:-1]:
            string_parts = []
            for part in parts[2:-1]:
                if isinstance(part, str):
                    string_parts.append(f"{q}{part}{q}")
                else:
                    part_expr, _, part_sources = self.get_expr(part)
                    string_parts.append(part_expr)
                    sources.extend(part_sources)
            string += " + " + " + ".join(string_parts)
        return string, sources

    def get_expr_ifthenelse(
        self, wdl_ifthenelse: WDL.Expr.IfThenElse
    ) -> tuple[str, list[str]]:
        """
        Translate WDL IfThenElse Expressions.

        :return: The CWL expresion and a list of sources
        """
        condition, _, sources = self.get_expr(wdl_ifthenelse.condition)
        if_true, _, sources_true = self.get_expr(wdl_ifthenelse.consequent)
        if_false, _, sources_false = self.get_expr(wdl_ifthenelse.alternative)
        sources.extend(sources_true)
        sources.extend(sources_false)
        return f"{condition} ? {if_true} : {if_false}", sources

    def get_expr_apply(
        self, wdl_apply_expr: WDL.Expr.Apply, top: bool
    ) -> tuple[str, list[str]]:
        """
        Translate WDL Apply Expressions.

        :return: The CWL Expression and a list of source names.
        """
        binary_ops = {
            "_gt": ">",
            "_land": "&&",
            "_lor": "||",
            "_lte": "<=",
            "_neq": "!==",
            "_lt": "<",
            "_mul": "*",
            "_eqeq": "===",
            "_div": "/",
            "_sub": "-",
        }
        single_arg_fn = {  # implemented elsewhere, just return the argument
            "read_string",
            "read_float",
            "glob",
            "read_int",
            "read_boolean",
            "read_tsv",
            "read_lines",
        }
        function_name = wdl_apply_expr.function_name
        arguments = wdl_apply_expr.arguments
        if not arguments:
            raise WDLSourceLine(wdl_apply_expr, ConversionException).makeError(
                f"The '{wdl_apply_expr}' expression has no arguments."
            )
        treat_as_optional = wdl_apply_expr.type.optional
        if function_name == "_add":
            add_left_operand = arguments[0]
            add_left_operand_value, _, left_sources = self.get_expr(add_left_operand)
            add_right_operand, _, right_sources = self.get_expr(arguments[1])
            add_sources = left_sources + right_sources
            if getattr(
                add_left_operand, "function_name", None
            ) == "basename" and isinstance(wdl_apply_expr.parent, WDL.Tree.Decl):
                referer = wdl_apply_expr.parent.name
                treat_as_optional = True if referer in self.non_static_values else False
            return (
                (
                    f"{add_left_operand_value} + {add_right_operand}"
                    if not treat_as_optional
                    else f"{get_input(referer)} === null ? {add_left_operand_value} + {add_right_operand} : {get_input(referer)}"
                ),
                add_sources,
            )
        elif function_name == "basename":
            if len(arguments) == 1:
                only_operand = arguments[0]
                is_file = isinstance(only_operand.type, WDL.Type.File)
                if isinstance(only_operand, WDL.Expr.Get) and isinstance(
                    only_operand.expr, WDL.Expr.Ident
                ):
                    only_operand_name = get_expr_name(only_operand.expr)
                    solo_basename_sources = [str(only_operand.expr.name)]
                else:
                    only_operand_name, _, solo_basename_sources = self.get_expr(
                        only_operand
                    )
                return (
                    (
                        f"{only_operand_name}.basename"
                        if is_file
                        else f"{only_operand_name}.split('/').reverse()[0]"
                    ),
                    solo_basename_sources,
                )
            else:
                basename_target, suffix = arguments
                is_file = isinstance(basename_target.type, WDL.Type.File)
                basename_sources: list[str] = []
                if isinstance(basename_target, WDL.Expr.Get):
                    if isinstance(basename_target.expr, WDL.Expr.Ident):
                        basename_target_name = get_expr_name(basename_target.expr)
                        basename_sources = [str(basename_target.expr.name)]
                    else:
                        basename_target_expr, _, basename_sources = self.get_expr(
                            basename_target.expr
                        )
                        basename_target_name = (
                            f"{basename_target_expr}.{basename_target.member}"
                        )
                elif isinstance(basename_target, WDL.Expr.Apply):
                    basename_target_name, _, basename_sources = self.get_expr(
                        basename_target, None, top
                    )
                suffix_str = str(get_literal_value(suffix))
                regex_str = re.escape(suffix_str)
                return (
                    (
                        f"{basename_target_name}.basename.replace(/{regex_str}$/, '') "
                        if is_file
                        else f"{basename_target_name}.split('/').reverse()[0].replace(/{regex_str}$/, '')"
                    ),
                    basename_sources,
                )
        elif function_name == "defined":
            only_operand = arguments[0]
            assert isinstance(only_operand, WDL.Expr.Get) and isinstance(  # nosec
                only_operand.expr, WDL.Expr.Ident
            )
            return f"{get_expr_name(only_operand.expr)} !== null", [
                str(only_operand.expr.name)
            ]
        elif function_name == "_interpolation_add":
            arg_value, arg_name = arguments
            iadd_sources: list[str] = []
            if isinstance(arg_name, WDL.Expr.String) and isinstance(
                arg_value, (WDL.Expr.Apply, WDL.Expr.String)
            ):
                value_expr, _, iadd_value_sources = self.get_expr(arg_value)
                name_expr, _, iadd_name_sources = self.get_expr(arg_name)
                return (
                    f"{value_expr} + {name_expr}",
                    iadd_value_sources + iadd_name_sources,
                )
            if isinstance(arg_name, (WDL.Expr.Placeholder, WDL.Expr.Get)):
                just_arg_name = get_expr_name(arg_name.expr)  # type: ignore[arg-type]
                iadd_sources.append(str(arg_name.expr.name))  # type: ignore[attr-defined]
                arg_name_with_file_check = get_expr_name_with_is_file_check(
                    arg_name.expr  # type: ignore
                )
            elif isinstance(arg_value, (WDL.Expr.Placeholder, WDL.Expr.Get)):
                just_arg_name = get_expr_name(arg_value.expr)  # type: ignore[arg-type]
                iadd_sources.append(str(arg_value.expr.name))  # type: ignore[attr-defined]
                arg_name_with_file_check = get_expr_name_with_is_file_check(
                    arg_value.expr  # type: ignore
                )
                arg_value = arg_name
            with WDLSourceLine(arg_value, ConversionException):
                arg_value_str, _, iadd_value_sources = self.get_expr(arg_value)
                iadd_sources.extend(iadd_value_sources)
                return (
                    (
                        f'{just_arg_name} === null ? "" : {arg_value_str} + {arg_name_with_file_check}'
                        if treat_as_optional
                        else f"{arg_value_str} + {arg_name_with_file_check}"
                    ),
                    iadd_sources,
                )
        elif function_name == "sub":
            wdl_apply, arg_string, arg_sub = arguments
            sub_expr, _, sub_sources = self.get_expr(wdl_apply, None, top)
            arg_string_expr, _, sub_string_sources = self.get_expr(arg_string)
            arg_sub_expr, _, sub_expr_sources = self.get_expr(arg_sub)
            sub_sources.extend(sub_string_sources)
            sub_sources.extend(sub_expr_sources)
            return (
                f"{sub_expr}.replace({arg_string_expr}, {arg_sub_expr}) ",
                sub_sources,
            )
        elif function_name == "_at":
            iterable_object, index = arguments
            iterable_object_expr, _, at_sources = self.get_expr(iterable_object)
            index_expr, _, at_index_sources = self.get_expr(index)
            at_sources.extend(at_index_sources)
            return f"{iterable_object_expr}[{index_expr}]", at_sources
        elif function_name in binary_ops:
            left_operand, right_operand = arguments
            left_operand_expr, _, bops_sources = self.get_expr(left_operand)
            right_operand_expr, _, bops_right_sources = self.get_expr(right_operand)
            bops_sources.extend(bops_right_sources)
            return (
                f"{left_operand_expr} {binary_ops[function_name]} {right_operand_expr}",
                bops_sources,
            )
        elif function_name == "length":
            only_arg_expr, _, lsources = self.get_expr_get(arguments[0], False)  # type: ignore
            return f"{only_arg_expr}.length", lsources
        elif function_name == "quote":
            qtarget = arguments[0]
            only_arg_expr, _, qsources = self.get_expr(qtarget)
            if isinstance(qtarget.type, WDL.Type.Array) and isinstance(
                qtarget.type.item_type, WDL.Type.File
            ):
                return (
                    only_arg_expr
                    + """.map(function(item) {return '\\"'+item.path+'\\"'})""",
                    qsources,
                )
            return (
                only_arg_expr + """.map(function(item) {return '\\"'+item+'\\"'})""",
                qsources,
            )
        elif function_name == "round":
            only_arg_expr, _, rsources = self.get_expr(arguments[0])
            return f"Math.round({only_arg_expr})", rsources
        elif function_name in single_arg_fn:
            only_arg = arguments[0]
            expression, _, saf_sources = self.get_expr(only_arg)
            return expression, saf_sources
        elif function_name == "select_first":
            array_obj = cast(WDL.Expr.Array, arguments[0])
            sf_sources = []
            array_items = []
            for item in array_obj.items:
                array_item, _, array_item_sources = self.get_expr(item)
                array_items.append(str(array_item))
                sf_sources.extend(array_item_sources)
            items_str = ", ".join(array_items)
            return (
                f"[{items_str}].find(function(element) {{ return element !== null }}) "
            ), sf_sources
        elif function_name == "select_all":
            array_obj = cast(WDL.Expr.Array, arguments[0])
            sa_sources: list[str] = []
            sa_array_items: list[str] = []
            for item in array_obj.items:
                item_expr, _, item_sources = self.get_expr(item)
                sa_array_items.append(str(item_expr))
                sa_sources.extend(item_sources)
            items_str = ", ".join(sa_array_items)
            return (
                f"[{items_str}].filter(function(element) {{ return element !== null }}) ",
                sa_sources,
            )
        elif function_name == "ceil":
            only_arg, _, csources = self.get_expr(arguments[0])  # type: ignore
            return f"Math.ceil({only_arg}) ", csources
        elif function_name == "size":
            ssources: list[str] = []
            if len(arguments) == 1:
                left_operand = arguments[0]
                unit_value = "1"
            else:
                left_operand, right_operand = arguments
                right_value = str(get_literal_value(right_operand))
                unit_base, unit_exponent = get_mem_in_bytes(right_value)
                unit_value = f"{unit_base}^{unit_exponent}"
            if isinstance(left_operand, WDL.Expr.Array):
                sarray_items: list[str] = []
                for item in left_operand.items:
                    item_expr, _, item_sources = self.get_expr(item)
                    sarray_items.append(item_expr)
                    ssources.extend(item_sources)
                left = ", ".join(sarray_items)
                left_str = f"[{left}]"
            else:
                left_str, _, ssources = self.get_expr(left_operand)
            return (
                "(function(size_of=0)"
                + "{"
                + f"{left_str}.forEach(function(element)"
                + "{ if (element) {"
                + "size_of += element.size"
                + "}})}"
                + f") / {unit_value}",
                ssources,
            )
        elif function_name == "flatten":
            flatten_array = arguments[0]
            with WDLSourceLine(flatten_array, ConversionException):
                items_str, _, fsources = self.get_expr(flatten_array)
            result = (
                "(function () {var new_array = []; "
                + items_str
                + ".forEach(function(value, index, obj) "
                "{value.forEach(function(sub_value, sub_index, sub_obj) "
                "{new_array.push(sub_value);});}); return new_array;})()"
            )
            return result, fsources
        elif function_name == "sep":
            sep, array = arguments
            if isinstance(array, (WDL.Expr.Get, WDL.Expr.Apply)) and isinstance(
                array.type, WDL.Type.Array
            ):
                item_type = array.type.item_type
            else:
                raise WDLSourceLine(array, ConversionException).makeError(
                    f"Unhandled sep array type: {type(array)}: {array}."
                )
            sep_str = get_literal_value(sep)
            if sep_str is None:
                sep_str, _, sep_sources = self.get_expr(sep)
            else:
                sep_str = f'"{sep_str}"'
                sep_sources = []
            if isinstance(item_type, WDL.Type.File):
                array_expr, _, sep_item_sources = self.get_expr(array)
                sep_sources.extend(sep_item_sources)
                return (
                    f"{array_expr}.map("
                    + "function(el) {return el.path}).join("
                    + sep_str
                    + ")",
                    sep_sources,
                )
            else:
                sep_expr, _, sep_item_sources = self.get_expr(array)
                sep_sources.extend(sep_item_sources)
                return f"{sep_expr}.join({sep_str})", sep_sources
        raise WDLSourceLine(wdl_apply_expr, ConversionException).makeError(
            f"Function name '{function_name}' not yet handled."
        )

    def get_expr_get(
        self, wdl_get_expr: WDL.Expr.Get, top: bool
    ) -> tuple[str, WDL.Type.Base, list[str]]:
        """
        Translate WDL Get Expressions.

        :return: The CWL Expression, the WDL target type, and a list of sources
        """
        member = wdl_get_expr.member

        if not member:
            expression, sources = self.get_expr_ident(wdl_get_expr.expr, top)  # type: ignore
            return expression, wdl_get_expr.type, sources
        struct_name, _, sources = self.get_expr(wdl_get_expr.expr)
        return f"{struct_name}.{member}", wdl_get_expr.type, sources

    def get_expr_ident(
        self, wdl_ident_expr: WDL.Expr.Ident, top: bool
    ) -> tuple[str, list[str]]:
        """
        Translate WDL Ident Expressions.

        :param top: True, if the wdl_ident_expr is a standalone (non-embedded)
                    WDL expression. Will be used to append a ".path" to the CWL
                    expression, if the type is File.
        :return: A CWL expression and a list of sources
        """
        id_name = wdl_ident_expr.name
        referee = wdl_ident_expr.referee
        optional = wdl_ident_expr.type.optional
        sources: list[str] = []
        if referee:
            with WDLSourceLine(referee, ConversionException):
                if isinstance(referee, WDL.Tree.Call):
                    return id_name, sources
                if isinstance(referee, WDL.Tree.Gather):
                    return "_".join(id_name.split(".")), [
                        "/".join(id_name.rsplit(".", maxsplit=1))
                    ]
                if referee.expr and (
                    wdl_ident_expr.name in self.optional_cwl_null
                    or wdl_ident_expr.name not in self.non_static_values
                ):
                    expression, _, sources = self.get_expr(referee.expr, None, top)
                    return expression, sources
        ident_name = get_input(id_name)
        sources.append(str(id_name))
        if optional and isinstance(wdl_ident_expr.type, WDL.Type.File):
            # To prevent null showing on the terminal for inputs of type File
            name_with_file_check = get_expr_name_with_is_file_check(wdl_ident_expr)
            return f'{ident_name} === null ? "" : {name_with_file_check}', sources
        return (
            f"{ident_name}.path"
            if (
                top is True
                and isinstance(wdl_ident_expr.type, WDL.Type.File)
                and ".path" not in ident_name
            )
            else ident_name
        ), sources

    def get_cpu_requirement(self, cpu_runtime: WDL.Expr.Base) -> Union[int, float, str]:
        """Translate WDL Runtime CPU requirement to CWL Resource Requirement."""
        if isinstance(cpu_runtime, (WDL.Expr.Int, WDL.Expr.Float)):
            return cast(Union[int, float], get_literal_value(cpu_runtime))
        elif isinstance(cpu_runtime, WDL.Expr.String):
            literal_str = cast(Any, get_literal_value(cpu_runtime))
            return int(literal_str) if "." not in literal_str else float(literal_str)
        return f"$({self.get_expr(cpu_runtime)[0]})"

    def get_cwl_command_requirements(
        self, wdl_commands: list[Union[str, WDL.Expr.Placeholder]]
    ) -> Optional[cwl.InitialWorkDirRequirement]:
        """Translate WDL commands into CWL Initial WorkDir Requirement."""
        command_str: str = ""
        for wdl_command in wdl_commands:
            if isinstance(wdl_command, str):
                command_str += wdl_command.replace("$(", "\\$(")
            else:
                command_str += (
                    f"$({self.translate_wdl_placeholder(wdl_command, True)[0]})"
                )
        command_str = textwrap.dedent(command_str)
        return (
            cwl.InitialWorkDirRequirement(
                listing=[cwl.Dirent(entry=command_str, entryname="script.bash")]
            )
            if len(command_str.strip()) > 0
            else None
        )

    def translate_wdl_placeholder(
        self, wdl_placeholder: WDL.Expr.Placeholder, top: bool = False
    ) -> tuple[str, Optional[WDL.Type.Base], list[str]]:
        """
        Translate WDL Expr Placeholder.

        :param top: `True`, if the wdl_placeholder is a standalone (non-embedded)
                    WDL expression. Will be used to append a ".path" to the CWL
                    expression, if the type is File.

        :return: A valid CWL expression, the WDL target type if known, and a list of sources
        """
        expr = wdl_placeholder.expr
        placeholder_expr, placeholder_expr_type, sources = self.get_expr(
            expr, None, top
        )
        if options := wdl_placeholder.options:
            if "true" in options:
                true_str = nice_quote(options["true"])
                false_str = nice_quote(options["false"])
                test_str = f"{placeholder_expr} ? {true_str} : {false_str}"
                is_optional = False
                if isinstance(expr, WDL.Expr.Get):
                    is_optional = expr.type.optional
                elif isinstance(expr, WDL.Expr.Apply):
                    is_optional = (
                        expr.arguments[0].type.optional
                        and expr.function_name != "defined"  # optimization
                    )
                if not is_optional:
                    return test_str, placeholder_expr_type, sources
                else:
                    if "default" in options:
                        return (
                            (
                                f"{placeholder_expr} === null ? "
                                f"{nice_quote(options['default'])} : {test_str}"
                            ),
                            placeholder_expr_type,
                            sources,
                        )
                    return (
                        f'{placeholder_expr} === null ? "" : {test_str}',
                        placeholder_expr_type,
                        sources,
                    )
            elif "sep" in options:
                separator = options["sep"]
                assert isinstance(expr.type, WDL.Type.Array)  # nosec
                item_type = expr.type.item_type
                if isinstance(item_type, WDL.Type.File):
                    pl_holder_str = (
                        placeholder_expr + ".map(function(el) {return el.path})"
                        f'.join("{separator}")'
                    )
                else:
                    pl_holder_str = f'{placeholder_expr}.join("{separator}")'
                if "default" in options and (expr.type.optional or item_type.optional):
                    return (
                        f"{placeholder_expr} === null ? "
                        f"{nice_quote(options['default'])} : {pl_holder_str}",
                        placeholder_expr_type,
                        sources,
                    )
                return pl_holder_str, placeholder_expr_type, sources
            # options must contain only "default", no "sep" or "true"/"false"
            return (
                (
                    f"{placeholder_expr} === null ? "
                    f"{nice_quote(options['default'])} : {placeholder_expr}"
                ),
                placeholder_expr_type,
                sources,
            )
        if (
            top is True
            and isinstance(placeholder_expr_type, WDL.Type.File)
            and ".path" not in placeholder_expr
        ):
            placeholder_expr += ".path"
        return placeholder_expr, placeholder_expr_type, sources

    def get_cwl_workflow_inputs(
        self,
        wdl_inputs: WDL.Env.Bindings[WDL.Tree.Decl],
        meta: Optional[dict[str, Any]] = None,
    ) -> list[cwl.WorkflowInputParameter]:
        """Convert WDL inputs into CWL inputs and return a list of CWL Workflow Input Parameters."""
        inputs: list[cwl.WorkflowInputParameter] = []

        for input_decl in wdl_inputs:
            input_name = input_decl.name
            if input_name.endswith("._runtime"):
                continue
            self.non_static_values.add(input_name)
            input_value = None
            wdl_input = input_decl.value
            type_of = self.get_cwl_type(
                wdl_input.type, cwl.InputArraySchema, cwl.InputRecordSchema, wdl_input
            )

            if wdl_input.type.optional or isinstance(wdl_input.expr, WDL.Expr.Apply):
                final_type_of: Union[
                    list[
                        Union[
                            str,
                            cwl.InputArraySchema,
                            cwl.InputRecordSchema,
                        ]
                    ],
                    str,
                    cwl.InputArraySchema,
                    cwl.InputRecordSchema,
                ] = [type_of, "null"]
            else:
                final_type_of = type_of

            if wdl_input.expr is not None:
                with WDLSourceLine(wdl_input.expr, ConversionException):
                    input_value = get_literal_value(wdl_input.expr)
                    if input_value and final_type_of == "float":
                        input_value = float(input_value)

            doc: Optional[str] = None
            if meta and input_name in meta:
                if isinstance(meta[input_name], str):
                    doc = meta[input_name]
                elif "description" in meta[input_name]:
                    doc = meta[input_name]["description"]
            inputs.append(
                cwl.WorkflowInputParameter(
                    id=input_name,
                    type_=final_type_of,
                    default=input_value,
                    doc=doc,
                )
            )

        return inputs

    def get_cwl_task_inputs(
        self,
        wdl_inputs: Optional[list[WDL.Tree.Decl]],
        meta: Optional[dict[str, Any]] = None,
    ) -> tuple[list[cwl.CommandInputParameter], list[str]]:
        """
        Convert WDL inputs into CWL inputs.

        Return a tuple: list of CWL Command Input Parameters and list of restriction checks.
        """
        inputs: list[cwl.CommandInputParameter] = []
        restriction_checks: list[str] = []

        if not wdl_inputs:
            return inputs, restriction_checks

        for wdl_input in wdl_inputs:
            input_name = wdl_input.name
            self.non_static_values.add(input_name)
            input_value = None

            type_of = self.get_cwl_type(
                wdl_input.type,
                cwl.CommandInputArraySchema,
                cwl.CommandInputRecordSchema,
                wdl_input,
            )

            if isinstance(wdl_input.type, WDL.Type.Array) and wdl_input.type.nonempty:
                restriction_checks.append(
                    f"if ({get_input(input_name)}.length == 0) "
                    "{throw "
                    f'"{input_name} must contain at least one item.";'
                    '} else { return "";}'
                )

            if wdl_input.type.optional or isinstance(wdl_input.expr, WDL.Expr.Apply):
                final_type_of: Union[
                    list[
                        Union[
                            str,
                            cwl.CommandInputArraySchema,
                            cwl.CommandInputRecordSchema,
                        ]
                    ],
                    str,
                    cwl.CommandInputArraySchema,
                    cwl.CommandInputRecordSchema,
                ] = [type_of, "null"]
                if isinstance(wdl_input.expr, WDL.Expr.Apply):
                    self.optional_cwl_null.add(input_name)
            else:
                final_type_of = type_of

            if wdl_input.expr is not None:
                if not isinstance(wdl_input.expr, WDL.Expr.Apply):
                    with WDLSourceLine(wdl_input.expr, ConversionException):
                        input_value = get_literal_value(wdl_input.expr)
                        if input_value and final_type_of == "float":
                            input_value = float(input_value)

            doc: Optional[str] = None
            if meta and input_name in meta:
                if isinstance(meta[input_name], str):
                    doc = meta[input_name]
                elif "description" in meta[input_name]:
                    doc = meta[input_name]["description"]
            inputs.append(
                cwl.CommandInputParameter(
                    id=input_name, type_=final_type_of, default=input_value, doc=doc
                )
            )

        return inputs, restriction_checks

    def get_struct_inputs(
        self, members: Optional[dict[str, WDL.Type.Base]]
    ) -> list[cwl.CommandInputRecordField]:
        """Get member items of a WDL struct and return a list of cwl.CommandInputRecordField."""
        inputs: list[cwl.CommandInputRecordField] = []
        if not members:
            return inputs
        for member, value in members.items():
            input_name = member
            type_of = self.get_cwl_type(
                value,
                cwl.CommandInputArraySchema,
                cwl.CommandInputRecordSchema,
                None,
            )
            inputs.append(cwl.CommandInputRecordField(name=input_name, type_=type_of))
        return inputs

    def set_cwl_task_outputs(
        self,
        wdl_outputs: list[WDL.Tree.Decl],
        meta: Optional[dict[str, Any]],
        tool: cwl.CommandLineTool,
    ) -> list[cwl.CommandOutputParameter]:
        """Convert WDL outputs into CWL outputs and return a list of CWL Command Output Parameters."""
        outputs: list[cwl.CommandOutputParameter] = []

        if not wdl_outputs:
            return outputs

        for wdl_output in wdl_outputs:
            output_name = wdl_output.name
            doc: Optional[str] = None
            if meta and output_name in meta:
                if isinstance(meta[output_name], str):
                    doc = meta[output_name]
                else:
                    if "description" in meta[output_name]:
                        doc = meta[output_name]["description"]
            type_of = self.get_cwl_type(
                wdl_output.type,
                cwl.CommandOutputArraySchema,
                cwl.CommandOutputRecordSchema,
                wdl_output,
            )
            glob = type_of == "File" or (
                isinstance(type_of, cwl.CommandOutputArraySchema)
                and type_of.items == "File"
            )

            if not wdl_output.expr:
                raise WDLSourceLine(wdl_output, ConversionException).makeError(
                    "Missing expression"
                )

            if (
                isinstance(wdl_output.expr, WDL.Expr.Apply)
                and wdl_output.expr.function_name in read_funcs
            ):
                if (
                    isinstance(wdl_output.expr.arguments[0], WDL.Expr.Apply)
                    and wdl_output.expr.arguments[0].function_name == "stdout"
                ):
                    glob_str = tool.stdout = "_stdout"
                else:
                    glob_expr, _, _ = self.get_expr(wdl_output.expr)
                    is_literal = wdl_output.expr.arguments[0].literal
                    if is_literal:
                        glob_str = str(get_literal_value(wdl_output.expr.arguments[0]))
                    else:
                        glob_str = f"$({glob_expr})"

                outputs.append(
                    cwl.CommandOutputParameter(
                        id=output_name,
                        doc=doc,
                        type_=type_of,
                        outputBinding=cwl.CommandOutputBinding(
                            glob=glob_str,
                            loadContents=True,
                            outputEval=read_funcs[wdl_output.expr.function_name],
                        ),
                    )
                )
            elif (
                isinstance(wdl_output.expr, WDL.Expr.Apply)
                and wdl_output.expr.function_name == "stdout"
            ):
                outputs.append(
                    cwl.CommandOutputParameter(
                        id=output_name,
                        doc=doc,
                        type_="stdout",
                    ),
                )
            else:
                if wdl_output.type.optional:
                    final_type_of: Union[
                        list[
                            Union[
                                str,
                                cwl.CommandOutputArraySchema,
                                cwl.CommandOutputRecordSchema,
                            ]
                        ],
                        str,
                        cwl.CommandInputArraySchema,
                        cwl.CommandOutputArraySchema,
                        cwl.CommandOutputRecordSchema,
                    ] = [type_of, "null"]
                else:
                    final_type_of = type_of
                if glob:
                    globs: list[str] = []
                    if isinstance(wdl_output.expr, WDL.Expr.Array):
                        targets: Sequence[WDL.Expr.Base] = wdl_output.expr.items
                    else:
                        targets = [wdl_output.expr]
                    for entry in targets:
                        glob_str = f"$({self.get_expr(entry, None, True)[0]})"
                        if (
                            isinstance(wdl_output.expr, WDL.Expr.String)
                            and wdl_output.expr.literal is not None
                        ):
                            glob_str = glob_str[3:-2]
                        globs.append(glob_str)
                    final_glob = globs if len(globs) > 1 else globs[0]
                    outputs.append(
                        cwl.CommandOutputParameter(
                            id=output_name,
                            doc=doc,
                            type_=final_type_of,
                            outputBinding=cwl.CommandOutputBinding(glob=final_glob),
                        )
                    )
                else:
                    outputEval = (
                        f"$({self.get_expr(wdl_output.expr, wdl_output.type)[0]})"
                    )
                    outputs.append(
                        cwl.CommandOutputParameter(
                            id=output_name,
                            doc=doc,
                            type_=final_type_of,
                            outputBinding=cwl.CommandOutputBinding(
                                outputEval=outputEval
                            ),
                        )
                    )
        return outputs


def main(args: Union[list[str], None] = None) -> None:
    """Entry point."""
    # Command-line parsing.
    parser = argparse.ArgumentParser(
        description="Converts WDL workflows into CWL workflows. Outputs "
        "to <stdout> by default."
    )
    parser.add_argument("workflow", help="Path to WDL workflow")
    parser.add_argument("-o", "--output", help="Name of output CWL file")
    parsed_args = parser.parse_args(args)

    cwl_result = convert(parsed_args.workflow)

    # Serialize result in YAML to either <stdout> or specified output file.
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.indent = 4
    yaml.block_seq_indent = 2
    scalarstring.walk_tree(cwl_result)

    if parsed_args.output is None:
        yaml.dump(cwl_result, sys.stdout)
    else:
        with open(parsed_args.output, "w") as f:
            yaml.dump(cwl_result, f)


if __name__ == "__main__":
    main(sys.argv[1:])
