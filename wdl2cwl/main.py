"""Main entrypoint for WDL2CWL."""
import os
import re
from typing import List, Union, Optional, Any, Set, Dict
import WDL
import cwl_utils.parser.cwl_v1_2 as cwl
import regex  # type: ignore

import textwrap
import sys
import argparse


from ruamel.yaml import scalarstring
from ruamel.yaml.main import YAML

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


def convert(doc: str) -> Dict[str, Any]:
    """Convert a WDL workflow, reading the file, into a CWL workflow Python object."""
    wdl_path = os.path.relpath(doc)
    doc_tree = WDL.load(wdl_path)

    parser = Converter()
    if doc_tree.workflow:
        return parser.load_wdl_workflow(doc_tree.workflow).save()
    if len(doc_tree.tasks) == 1:
        return parser.load_wdl_objects(doc_tree.tasks[0]).save()
    else:
        return {
            "cwlVersion": "v1.2",
            "$graph": [parser.load_wdl_objects(task).save() for task in doc_tree.tasks],
        }


class Converter:
    """Object that handles WDL Workflows and task conversion to CWL."""

    def __init__(self) -> None:
        """Initialize the sets used by the object and prevent inconsistent behaviours."""
        self.non_static_values: Set[str] = set()
        self.optional_cwl_null: Set[str] = set()

    def load_wdl_objects(
        self, obj: Union[WDL.Tree.Task, WDL.Tree.Workflow]
    ) -> cwl.CommandLineTool:
        """Load a WDL SourceNode obj and returns either a Task or a Workflow."""
        if isinstance(obj, WDL.Tree.Task):
            return self.load_wdl_task(obj)
        raise Exception(f"Unimplemented type: {type(obj)}: {obj}")

    def load_wdl_workflow(self, obj: WDL.Tree.Workflow) -> cwl.Workflow:
        """Load WDL workflow and convert to CWL."""
        inputs: List[cwl.WorkflowInputParameter] = []
        outputs: List[cwl.WorkflowOutputParameter] = []
        wf_steps: List[cwl.WorkflowStep] = []
        wf_name = obj.name
        wf_description = obj.meta["description"]
        for call in obj.body:
            call_name = call.name  # type: ignore
            callee = call.callee  # type: ignore
            namespace, _ = call.callee_id  # type: ignore
            cwl_call_inputs = self.get_cwl_task_inputs(callee.inputs)
            wf_step_inputs = [
                cwl.WorkflowStepInput(id=x.id, source=f"{namespace}.{call_name}.{x.id}")
                for x in cwl_call_inputs
            ]
            inputs.extend(
                [
                    cwl.WorkflowInputParameter(
                        id=f"{namespace}.{call_name}.{x.id}",
                        type=x.type,
                        default=x.default,
                    )
                    for x in cwl_call_inputs
                ]
            )
            cwl_call_ouputs = self.get_cwl_task_outputs(callee.outputs)
            wf_step_outputs = [cwl.WorkflowStepOutput(id=x.id) for x in cwl_call_ouputs]
            outputs.extend(
                [
                    cwl.WorkflowOutputParameter(
                        id=x.id,
                        type=x.type,
                        outputSource=f"{namespace}.{call_name}/{x.id}",
                    )
                    for x in cwl_call_ouputs
                ]
            )
            wf_step_run = self.load_wdl_objects(callee)
            wf_step = cwl.WorkflowStep(
                wf_step_inputs,
                id=f"{namespace}.{call_name}",
                run=wf_step_run,
                out=wf_step_outputs,
            )
            wf_steps.append(wf_step)

        return cwl.Workflow(
            id=wf_name,
            cwlVersion="v1.2",
            doc=wf_description,
            inputs=inputs,
            steps=wf_steps,
            outputs=outputs,
        )

    def load_wdl_task(self, obj: WDL.Tree.Task) -> cwl.CommandLineTool:
        """Load task and convert to CWL."""
        cwl_inputs = self.get_cwl_task_inputs(obj.inputs)
        cwl_outputs = self.get_cwl_task_outputs(obj.outputs)
        docker_requirement = (
            self.get_cwl_docker_requirements(obj.runtime["docker"])  # type: ignore
            if "docker" in obj.runtime
            else None
        )
        cwl_command_str = self.get_cwl_command_requirements(obj.command.parts)
        base_command = ["bash", "example.sh"]
        requirements: List[cwl.ProcessRequirement] = []
        if docker_requirement:
            requirements.append(docker_requirement)
        if cwl_command_str:
            requirements.append(cwl_command_str)
        requirements.append(cwl.InlineJavascriptRequirement())
        requirements.append(cwl.NetworkAccess(networkAccess=True))
        cpu_requirement = (
            self.get_cpu_requirement(obj.runtime["cpu"])
            if "cpu" in obj.runtime
            else None
        )
        memory_requirement = (
            self.get_memory_requirement(obj.runtime["memory"])  # type: ignore
            if "memory" in obj.runtime
            else None
        )
        outdir_requirement = (
            self.get_outdir_requirement(obj.runtime["disks"])  # type: ignore
            if "disks" in obj.runtime
            else 1024
        )
        requirements.append(
            cwl.ResourceRequirement(
                coresMin=cpu_requirement,
                ramMin=memory_requirement,
                outdirMin=outdir_requirement,
            )
        )
        time_minutes = (
            self.get_time_minutes_requirement(obj.runtime["time_minutes"])  # type: ignore
            if "time_minutes" in obj.runtime
            else None
        )
        if time_minutes:
            requirements.append(
                cwl.ToolTimeLimit(
                    timelimit=time_minutes,
                )
            )
        runtime_requirements = ["docker", "memory", "disks", "time_minutes", "cpu"]

        for i in runtime_requirements:
            if i not in obj.runtime:
                print(
                    "----WARNING: SKIPPING REQUIREMENT " + i + "----", file=sys.stderr
                )
        if not obj.parameter_meta:
            print("----WARNING: SKIPPING PARAMETER_META----", file=sys.stderr)

        if not obj.meta:
            print("----WARNING: SKIPPING META----", file=sys.stderr)
        if len(obj.postinputs) > 0:
            for a in obj.postinputs:
                print(
                    "----WARNING: SKIPPING VARIABLE " + a.name + "----", file=sys.stderr
                )

        return cwl.CommandLineTool(
            id=obj.name,
            inputs=cwl_inputs,
            requirements=requirements,
            outputs=cwl_outputs,
            cwlVersion="v1.2",
            baseCommand=base_command,
        )

    def get_time_minutes_requirement(
        self, time_minutes: WDL.Expr.Get
    ) -> Union[str, int]:
        """Produce the time limit expression from WDL runtime time minutes."""
        if isinstance(time_minutes, (WDL.Expr.Int, WDL.Expr.Float)):
            literal = time_minutes.literal.value  # type: ignore
            return literal * 60  # type: ignore
        time_minutes_str = self.get_expr(time_minutes)
        return f"$({time_minutes_str} * 60)"

    def get_outdir_requirement(
        self, outdir: Union[WDL.Expr.Get, WDL.Expr.Apply]
    ) -> int:
        """Produce the memory requirement for the output directory from WDL runtime disks."""
        # This is yet to be implemented. After Feature Parity.
        return int(outdir.literal.value) * 1024  # type: ignore

    def get_input(self, input_name: str) -> str:
        """Produce a consise, valid CWL expr/param reference lookup string for a given input name."""
        if valid_js_identifier.match(input_name):
            return f"inputs.{input_name}"
        return f'inputs["{input_name}"]'  # pragma: no cover

    def get_memory_requirement(
        self, memory_runtime: Union[WDL.Expr.Ident, WDL.Expr.Get, WDL.Expr.String]
    ) -> Union[str, float]:
        """Translate WDL Runtime Memory requirement to CWL Resource Requirement."""
        if isinstance(memory_runtime, WDL.Expr.String):
            ram_min_literal = self.get_memory_literal(memory_runtime)
            return ram_min_literal
        ram_min = self.get_expr_name(memory_runtime.expr)  # type: ignore
        return self.get_ram_min_js(ram_min, "")

    def get_memory_literal(self, memory_runtime: WDL.Expr.String) -> float:
        """Get the literal value for memory requirement with type WDL.Expr.String."""
        if memory_runtime.literal is None:
            _, placeholder, unit, _ = memory_runtime.parts
            value_name = self.get_expr_get(placeholder.expr)  # type: ignore
            return self.get_ram_min_js(value_name, unit.strip())  # type: ignore

        ram_min = self.get_expr_string(memory_runtime)[1:-1]
        unit = re.search(r"[a-zA-Z]+", ram_min).group()  # type: ignore
        value = float(ram_min.split(unit)[0])

        if unit == "KiB":
            memory = value / 1024
        elif unit == "MiB":
            memory = value
        elif unit == "GiB":
            memory = value * 1024
        elif unit == "TiB":
            memory = value * 1024 * 1024
        elif unit == "B":
            memory = value / (1024 * 1024)
        elif unit == "KB" or unit == "K":
            memory = (value * 1000) / (1024 * 1024)
        elif unit == "MB" or unit == "M":
            memory = (value * (1000 * 1000)) / (1024 * 1024)
        elif unit == "GB" or unit == "G":
            memory = (value * (1000 * 1000 * 1000)) / (1024 * 1024)
        elif unit == "TB" or unit == "T":
            memory = (value * (1000 * 1000 * 1000 * 1000)) / (1024 * 1024)

        return memory

    def get_ram_min_js(self, ram_min_ref_name: str, unit: str) -> str:
        """Get memory requirement for user input."""
        append_str: str = ""
        if unit:
            append_str = '${\nvar unit = "' + unit + '";'  # pragma: no cover
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
            + "return parseInt(memory);\n}"
        )

        return js_str

    def get_expr(self, wdl_expr: Any) -> str:
        """Translate WDL Expressions."""
        if isinstance(wdl_expr, WDL.Expr.Apply):
            return self.get_expr_apply(wdl_expr)
        elif isinstance(wdl_expr, WDL.Expr.Get):
            return self.get_expr_get(wdl_expr)
        elif isinstance(wdl_expr, WDL.Expr.IfThenElse):
            return self.get_expr_ifthenelse(wdl_expr)
        elif isinstance(wdl_expr, WDL.Expr.Placeholder):
            return self.translate_wdl_placeholder(wdl_expr)
        elif isinstance(wdl_expr, WDL.Expr.String):
            return self.get_expr_string(wdl_expr)
        elif isinstance(wdl_expr, WDL.Tree.Decl):
            return self.get_expr(wdl_expr.expr)
        elif isinstance(
            wdl_expr,
            (
                WDL.Expr.Boolean,
                WDL.Expr.Int,
                WDL.Expr.Float,
                WDL.Expr.Array,
            ),
        ):
            return self.get_literal_name(wdl_expr)
        else:
            raise Exception(f"The expression '{wdl_expr}' is not handled yet.")

    def get_literal_name(
        self,
        expr: Union[
            WDL.Expr.Boolean,
            WDL.Expr.Int,
            WDL.Expr.Float,
            WDL.Expr.Array,
        ],
    ) -> str:
        """Translate WDL Boolean, Int or Float Expression."""
        if expr is None or not hasattr(expr, "parent"):
            raise Exception(f"{type(expr)} has no attribute 'parent'")
        # if the literal expr is used inside WDL.Expr.Apply
        # the literal value is what's needed
        if isinstance(expr.parent, WDL.Expr.Apply):  # type: ignore
            return expr.literal.value  # type: ignore
        parent_name = expr.parent.name  # type: ignore
        return self.get_input(parent_name)
        # raise Exception(f"The parent expression for {expr} is not WDL.Expr.Apply")

    def get_expr_string(self, wdl_expr_string: WDL.Expr.String) -> str:
        """Translate WDL String Expressions."""
        if wdl_expr_string.literal is not None:
            return f'"{wdl_expr_string.literal.value}"'
        string = ""
        parts = wdl_expr_string.parts
        for index, part in enumerate(parts[1:-1], start=1):
            if isinstance(
                part,
                (WDL.Expr.Placeholder, WDL.Expr.Apply, WDL.Expr.Get, WDL.Expr.Ident),
            ):
                placeholder = self.get_expr(part)
                part = (
                    "" if parts[index - 1] == '"' or parts[index - 1] == "'" else "' + "  # type: ignore
                )
                part += placeholder
                part += (
                    "" if parts[index + 1] == '"' or parts[index + 1] == "'" else " + '"  # type: ignore
                )
            string += part
        # condition to determine if the opening and closing quotes should be added to string
        # for cases where a placeholder begins or ends a WDL.Expr.String
        if type(parts[1]) == str:
            string = "'" + string
        if type(parts[-2]) == str:
            string = string + "'"
        return string

    def get_expr_ifthenelse(self, wdl_ifthenelse: WDL.Expr.IfThenElse) -> str:
        """Translate WDL IfThenElse Expressions."""
        condition = wdl_ifthenelse.condition
        if_true = wdl_ifthenelse.consequent
        if_false = wdl_ifthenelse.alternative

        condition = self.get_expr(condition)  # type: ignore
        if_true = self.get_expr(if_true)  # type: ignore
        if_false = self.get_expr(if_false)  # type: ignore
        return f"{condition} ? {if_true} : {if_false}"

    def get_expr_apply(self, wdl_apply_expr: WDL.Expr.Apply) -> str:  # type: ignore
        """Translate WDL Apply Expressions."""
        function_name = wdl_apply_expr.function_name
        arguments = wdl_apply_expr.arguments
        if not arguments:
            raise Exception(f"The '{wdl_apply_expr}' expression has no arguments.")
        treat_as_optional = wdl_apply_expr.type.optional
        if function_name == "_add":
            left_operand, right_operand = arguments
            right_operand = self.get_expr(right_operand)  # type: ignore
            left_operand_value = self.get_expr(left_operand)
            if getattr(left_operand, "function_name", None) == "basename":
                treat_as_optional = True
                referer = wdl_apply_expr.parent.name  # type: ignore
            return (
                f"{left_operand_value} + {right_operand}"
                if not treat_as_optional
                else f"{self.get_input(referer)} === null ? {left_operand_value} + {right_operand} : {self.get_input(referer)}"
            )
        elif function_name == "basename":
            if len(arguments) == 1:
                only_operand = arguments[0]
                is_file = isinstance(only_operand.type, WDL.Type.File)
                only_operand = self.get_expr_name(only_operand.expr)  # type: ignore
                return (
                    f"{only_operand}.basename"
                    if is_file
                    else f"{only_operand}.split('/').reverse()[0]"
                )
            elif len(arguments) == 2:
                operand, suffix = arguments
                is_file = isinstance(operand.type, WDL.Type.File)
                operand = self.get_expr_name(operand.expr)  # type: ignore
                suffix_str = suffix.literal.value  # type: ignore
                regex_str = re.escape(suffix_str)
                return (
                    f"{operand}.basename.replace(/{regex_str}$/, '') "
                    if is_file
                    else f"{operand}.split('/').reverse()[0].replace(/{regex_str}$/, '')"
                )
        elif function_name == "defined":
            only_operand = arguments[0]
            only_operand = self.get_expr_name(only_operand.expr)  # type: ignore
            return only_operand  # type: ignore
        elif function_name == "_interpolation_add":
            arg_value, arg_name = arguments
            if isinstance(arg_name, WDL.Expr.String) and isinstance(
                arg_value, WDL.Expr.Apply
            ):
                arg_name = self.get_expr(arg_name)  # type: ignore
                arg_value = self.get_expr_apply(arg_value)  # type: ignore
                return self.get_pseudo_interpolation_add(arg_value, arg_name)  # type: ignore
            just_arg_name = self.get_expr_name(arg_name.expr)  # type: ignore
            arg_name_with_file_check = self.get_expr_name_with_is_file_check(
                arg_name.expr  # type: ignore
            )
            arg_value = arg_value.literal.value  # type: ignore
            return (
                f'{just_arg_name} === null ? "" : "{arg_value}" + {arg_name_with_file_check}'
                if treat_as_optional
                else f"{arg_value} $({arg_name_with_file_check})"
            )
        elif function_name == "sub":
            wdl_apply, arg_string, arg_sub = arguments
            wdl_apply = self.get_expr(wdl_apply)  # type: ignore
            arg_string = self.get_expr(arg_string)  # type: ignore
            arg_sub = self.get_expr(arg_sub)  # type: ignore
            return f"{wdl_apply}.replace({arg_string}, {arg_sub}) "

        elif function_name == "_at":
            iterable_object, index = arguments
            iterable_object, index = self.get_expr(iterable_object), self.get_expr(  # type: ignore
                index
            )
            return f"{iterable_object}[{index}]"
        elif function_name == "_gt":
            left_operand, right_operand = arguments
            left_operand = self.get_expr_apply(left_operand)  # type: ignore
            right_operand = self.get_expr(right_operand)  # type: ignore
            return f"{left_operand} > {right_operand}"
        elif function_name == "length":
            only_arg = arguments[0]
            only_arg = self.get_expr_get(only_arg)  # type: ignore
            return f"{only_arg}.length"
        elif function_name == "_neq":
            left_operand, right_operand = arguments
            if isinstance(left_operand, WDL.Expr.Apply):
                left_operand = self.get_expr_apply(left_operand)  # type: ignore
            if isinstance(right_operand, WDL.Expr.Apply):
                right_operand = self.get_expr_apply(right_operand)  # type: ignore
            return f"{left_operand} !== {right_operand}"
        elif function_name == "read_string":
            only_arg = arguments[0]
            only_arg = self.get_expr(only_arg)  # type: ignore
            return only_arg  # type: ignore
        elif function_name == "glob":
            only_arg = arguments[0]
            glob = self.get_expr(only_arg)
            return glob

        else:
            raise ValueError(f"Function name '{function_name}' not yet handled.")

    def get_expr_get(self, wdl_get_expr: WDL.Expr.Get) -> str:
        """Translate WDL Get Expressions."""
        member = wdl_get_expr.member
        if (
            not member
            and isinstance(wdl_get_expr.expr, WDL.Expr.Ident)
            and wdl_get_expr.expr
        ):
            return self.get_expr_ident(wdl_get_expr.expr)
        raise Exception(f"Get expressions with {member} are not yet handled.")

    def get_expr_ident(self, wdl_ident_expr: WDL.Expr.Ident) -> str:
        """Translate WDL Ident Expressions."""
        ident_name = wdl_ident_expr.name
        ident_name = self.get_input(ident_name)
        referee = wdl_ident_expr.referee
        optional = wdl_ident_expr.type.optional
        if referee and referee.expr:
            if (
                wdl_ident_expr.name in self.optional_cwl_null
                or wdl_ident_expr.name not in self.non_static_values
            ):
                return self.get_expr(referee.expr)
        if optional and isinstance(wdl_ident_expr.type, WDL.Type.File):
            # To prevent null showing on the terminal for inputs of type File
            name_with_file_check = self.get_expr_name_with_is_file_check(wdl_ident_expr)
            return f'{ident_name} === null ? "" : {name_with_file_check}'
        return (
            ident_name
            if not isinstance(wdl_ident_expr.type, WDL.Type.File)
            else f"{ident_name}.path"
        )

    def get_pseudo_interpolation_add(
        self, left_operand: str, right_operand: str
    ) -> str:
        """Combine two strings in a _add function manner."""
        return f"{left_operand} + {right_operand}"

    def get_cpu_requirement(self, cpu_runtime: WDL.Expr.Base) -> str:
        """Translate WDL Runtime CPU requirement to CWL Resource Requirement."""
        if isinstance(cpu_runtime, (WDL.Expr.Int, WDL.Expr.Float)):
            cpu_str = cpu_runtime.literal.value  # type: ignore
            return cpu_str  # type: ignore
        elif isinstance(cpu_runtime, WDL.Expr.String):
            if cpu_runtime.literal is not None:
                literal_str = cpu_runtime.literal.value
                numeral = (
                    int(literal_str) if "." not in literal_str else float(literal_str)
                )
                return numeral  # type: ignore
        cpu_str = self.get_expr(cpu_runtime)
        return f"$({cpu_str})"

    def get_cwl_docker_requirements(
        self, wdl_docker: Union[WDL.Expr.Get, WDL.Expr.String]
    ) -> cwl.ProcessRequirement:
        """Translate WDL Runtime Docker requirements to CWL Docker Requirement."""
        if isinstance(wdl_docker, WDL.Expr.String):
            dockerpull = wdl_docker.literal.value  # type: ignore
        else:
            dockerpull_expr = wdl_docker.expr
            if dockerpull_expr is None or not isinstance(
                dockerpull_expr, WDL.Expr.Ident
            ):
                raise Exception(
                    f"Unsupported type: {type(dockerpull_expr)}: {dockerpull_expr}"
                )
            dockerpull_referee = dockerpull_expr.referee
            if dockerpull_referee is None:
                raise Exception(f"Unsupported type: {type(dockerpull_referee)}")
            dockerpull = dockerpull_referee.expr.literal.value
        return cwl.DockerRequirement(dockerPull=dockerpull)

    def get_cwl_command_requirements(
        self, wdl_commands: List[Union[str, WDL.Expr.Placeholder]]
    ) -> cwl.InitialWorkDirRequirement:
        """Translate WDL commands into CWL Initial WorkDir REquirement."""
        command_str: str = ""
        for wdl_command in wdl_commands:
            if isinstance(wdl_command, str):
                command_str += wdl_command.replace("$(", "\\$(")
            elif isinstance(wdl_command, WDL.Expr.Placeholder):
                command_str += self.translate_wdl_placeholder(wdl_command)

        command_str = textwrap.dedent(command_str)
        return cwl.InitialWorkDirRequirement(
            listing=[cwl.Dirent(entry=command_str, entryname="example.sh")]
        )

    def translate_wdl_placeholder(self, wdl_placeholder: WDL.Expr.Placeholder) -> str:
        """Translate WDL Expr Placeholder to a valid CWL command string."""
        cwl_command_str = ""
        expr = wdl_placeholder.expr
        if expr is None:
            raise Exception(f"Placeholder '{wdl_placeholder}' has no expr.")
        placeholder_expr = self.get_expr(expr)
        options = wdl_placeholder.options
        if options:
            if "true" in options:
                true_value = options["true"]
                false_value = options["false"]
                true_str = (
                    f'"{true_value}"' if '"' not in true_value else f"'{true_value}'"
                )
                false_str = (
                    f'"{false_value}"' if '"' not in false_value else f"'{false_value}'"
                )
                is_optional = False
                if isinstance(expr, WDL.Expr.Get):
                    is_optional = expr.type.optional
                elif isinstance(expr, WDL.Expr.Apply):
                    is_optional = expr.arguments[0].type.optional
                if not is_optional:
                    cwl_command_str = (
                        f"$({placeholder_expr} ? {true_str} : {false_str})"
                    )
                else:
                    cwl_command_str = (
                        f"$({placeholder_expr} === null ? {false_str} : {true_str})"
                    )
            elif "sep" in options:
                seperator = options["sep"]
                if isinstance(expr.type, WDL.Type.Array):
                    item_type = expr.type.item_type
                    if isinstance(item_type, WDL.Type.String):
                        cwl_command_str = f'$({placeholder_expr}.join("{seperator}"))'
                    elif isinstance(item_type, WDL.Type.File):
                        cwl_command_str = (
                            f"$({placeholder_expr}.map("
                            + 'function(el) {return el.path}).join("'
                            + seperator
                            + '"))'
                        )
                else:
                    raise Exception(
                        f"{wdl_placeholder} with expr of type {expr.type} is not yet handled"
                    )
            else:
                raise Exception(
                    f"Placeholders with options {options} are not yet handled."
                )
        else:
            # for the one case where the $(input.some_input_name) is used within the placeholder_expr
            # we return the placholder_expr without enclosing in another $()
            cwl_command_str = (
                f"$({placeholder_expr})"
                if placeholder_expr[-1] != ")"
                else placeholder_expr
            )
        # sometimes placeholders are used inside WDL.Expr.String.
        # with the parent and grand_parent we can confirm that we are in
        # the command string (WDL.Expr.String) and task (WDL.Tree.Task) respectively
        parent = wdl_placeholder.parent  # type: ignore
        grand_parent = parent.parent
        return (
            cwl_command_str
            if isinstance(parent, WDL.Expr.String)
            and isinstance(grand_parent, WDL.Tree.Task)
            else cwl_command_str[2:-1]
        )

    def get_expr_name(self, wdl_expr: WDL.Expr.Ident) -> str:
        """Extract name from WDL expr."""
        if wdl_expr is None or not hasattr(wdl_expr, "name"):
            raise Exception(f"{type(wdl_expr)} has not attribute 'name'")
        expr_name = self.get_input(wdl_expr.name)
        return expr_name

    def get_expr_name_with_is_file_check(self, wdl_expr: WDL.Expr.Ident) -> str:
        """Extract name from WDL expr and check if it's a file path."""
        if wdl_expr is None or not hasattr(wdl_expr, "name"):
            raise Exception(f"{type(wdl_expr)} has not attribute 'name'")
        expr_name = self.get_input(wdl_expr.name)
        is_file = isinstance(wdl_expr.type, WDL.Type.File)
        return expr_name if not is_file else f"{expr_name}.path"

    def get_cwl_task_inputs(
        self, wdl_inputs: Optional[List[WDL.Tree.Decl]]
    ) -> List[cwl.CommandInputParameter]:
        """Convert WDL inputs into CWL inputs and return a list of CWL Command Input Paramenters."""
        inputs: List[cwl.CommandInputParameter] = []

        if not wdl_inputs:
            return inputs

        for wdl_input in wdl_inputs:
            input_name = wdl_input.name
            self.non_static_values.add(input_name)
            input_value = None
            type_of: Union[str, cwl.CommandInputArraySchema]

            if isinstance(wdl_input.type, WDL.Type.Array):
                array_items_type = wdl_input.type.item_type
                input_type = self.get_cwl_type(array_items_type)  # type: ignore
                type_of = cwl.CommandInputArraySchema(items=input_type, type="array")
            else:
                type_of = self.get_cwl_type(wdl_input.type)  # type: ignore

            if wdl_input.type.optional or isinstance(wdl_input.expr, WDL.Expr.Apply):
                final_type_of: Union[
                    List[Union[str, cwl.CommandInputArraySchema]],
                    str,
                    cwl.CommandInputArraySchema,
                ] = [type_of, "null"]
                if isinstance(wdl_input.expr, WDL.Expr.Apply):
                    self.optional_cwl_null.add(input_name)
            else:
                final_type_of = type_of

            if wdl_input.expr is not None:
                if isinstance(wdl_input.expr, WDL.Expr.Apply):
                    input_value = None
                else:
                    input_value = wdl_input.expr.literal.value  # type: ignore
                    if final_type_of == "float":
                        input_value = float(input_value)

            inputs.append(
                cwl.CommandInputParameter(
                    id=input_name, type=final_type_of, default=input_value
                )
            )

        return inputs

    def get_cwl_type(self, input_type: WDL.Tree.Decl) -> str:
        """Determine the CWL type for a WDL input declaration."""
        if isinstance(input_type, WDL.Type.File):
            type_of = "File"
        elif isinstance(input_type, WDL.Type.String):
            type_of = "string"
        elif isinstance(input_type, WDL.Type.Boolean):
            type_of = "boolean"
        elif isinstance(input_type, WDL.Type.Int):
            type_of = "int"
        elif isinstance(input_type, WDL.Type.Float):
            type_of = "float"
        else:
            raise Exception(f"Input of type {input_type} is not yet handled.")
        return type_of

    def get_cwl_task_outputs(
        self, wdl_outputs: List[WDL.Tree.Decl]
    ) -> List[cwl.CommandOutputParameter]:
        """Convert WDL outputs into CWL outputs and return a list of CWL Command Output Parameters."""
        outputs: List[cwl.CommandOutputParameter] = []

        if not wdl_outputs:
            return outputs

        for wdl_output in wdl_outputs:
            output_name = wdl_output.name
            if isinstance(wdl_output.type, WDL.Type.Array):
                array_items_type = wdl_output.type.item_type
                input_type = self.get_cwl_type(array_items_type)  # type: ignore
                type_of = cwl.CommandOutputArraySchema(items=input_type, type="array")
            else:
                type_of = self.get_cwl_type(wdl_output.type)  # type: ignore

            if not wdl_output.expr:
                raise ValueError("Missing expression")

            if (
                isinstance(wdl_output.expr, WDL.Expr.Apply)
                and wdl_output.expr.function_name == "read_string"
            ):
                glob_expr = self.get_expr(wdl_output)
                is_literal = wdl_output.expr.arguments[0].literal
                if is_literal:
                    glob_str = glob_expr[
                        1:-1
                    ]  # remove quotes from the string returned by get_expr_string
                else:
                    glob_expr = f"$({glob_expr})"

                outputs.append(
                    cwl.CommandOutputParameter(
                        id=output_name,
                        type=type_of,
                        outputBinding=cwl.CommandOutputBinding(
                            glob=glob_str,
                            loadContents=True,
                            outputEval=r"$(self[0].contents.replace(/[\r\n]+$/, ''))",
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
                        type="stdout",
                    )
                )
            else:
                glob_expr = self.get_expr(wdl_output)
                glob_str = f"$({glob_expr})"

                if wdl_output.type.optional:
                    final_type_of: Union[
                        List[Union[str, cwl.CommandOutputArraySchema]],
                        str,
                        cwl.CommandInputArraySchema,
                    ] = [type_of, "null"]
                else:
                    final_type_of = type_of  # type: ignore
                if (
                    isinstance(wdl_output.expr, WDL.Expr.String)
                    and wdl_output.expr.literal is not None
                ):
                    glob_str = glob_str[3:-2]

                outputs.append(
                    cwl.CommandOutputParameter(
                        id=output_name,
                        type=final_type_of,
                        outputBinding=cwl.CommandOutputBinding(glob=glob_str),
                    )
                )
        return outputs


def main(args: Union[List[str], None] = None) -> None:
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

    main(sys.argv[1:])  # pragma: no cover
