# -*- coding: utf-8 -*-

#Copyright (c) 2019 André Santos
#
#Permission is hereby granted, free of charge, to any person obtaining a copy
#of this software and associated documentation files (the "Software"), to deal
#in the Software without restriction, including without limitation the rights
#to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#copies of the Software, and to permit persons to whom the Software is
#furnished to do so, subject to the following conditions:

#The above copyright notice and this permission notice shall be included in
#all copies or substantial portions of the Software.

#THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
#THE SOFTWARE.


###############################################################################
# Imports
###############################################################################

from builtins import range # Python 2 and 3: forward-compatible
from collections import namedtuple
from itertools import chain as iterchain
import os

from haros.hpl.hpl_ast import (
    HplVacuousTruth, HplBinaryOperator, HplLiteral, HplUnaryOperator
)
from haros.hpl.ros_types import get_type # FIXME
from jinja2 import Environment, PackageLoader

from .events import MonitorTemplate
from .data import (
    MessageStrategyGenerator, CyclicDependencyError, InvalidFieldOperatorError,
)
from .selectors import Selector


###############################################################################
# Constants
###############################################################################

KEY = "haros_plugin_pbt_gen"

EMPTY_DICT = {}

INF = float("inf")


################################################################################
# Plugin Entry Point
################################################################################

config_num = 0

def configuration_analysis(iface, config):
    if (not config.launch_commands or not config.nodes.enabled
            or not config.hpl_properties:):
        return
    settings = config.user_attributes.get(KEY, EMPTY_DICT)
    _validate_settings(iface, settings)
    try:
        global config_num
        config_num += 1
        gen = TestGenerator(iface, config, settings)
        gen.make_tests(config_num)
    except SpecError as e:
        iface.log_error(e.message)


def _validate_settings(iface, settings):
    # TODO fill in the rest
    msg = "invalid setting for '{}': {}; expected one of {}; assuming {}"
    val = settings.get("extra_monitors")
    exp = (None, True, False, "safety")
    default = True
    if val not in exp:
        iface.log_warning(msg.format(val, exp, default))
        settings["extra_monitors"] = default


################################################################################
# Data Structures
################################################################################

PublisherTemplate = namedtuple("PublisherTemplate",
    ("topic", "type_token", "rospy_type"))

TestTemplate = namedtuple("TestTemplate", ("monitor", "custom_strategies",
    "publishers", "subscribers", "pkg_imports", "property_text"))

Subscriber = namedtuple("Subscriber", ("topic", "type_token", "fake"))

MsgStrategy = namedtuple("MsgStrategy",
    ("name", "args", "pkg", "msg", "statements", "is_default"))


class SpecError(Exception):
    pass


class StrategyError(Exception):
    pass


################################################################################
# Test Generator
################################################################################

class TestGenerator(object):
    def __init__(self, iface, config, settings):
        self.iface = iface
        self.config = config
        self.settings = settings
        self.commands = config.launch_commands
        self.subbed_topics = self._get_open_subbed_topics()
        self.pubbed_topics = self._get_all_pubbed_topics()
        self.subscribers = self._get_subscribers()
        assumptions = []
        for p in config.hpl_assumptions:
            if p.is_fully_typed():
                assumptions.append(p)
            else:
                self.iface.log_warning(
                    "Skipping untyped assumption:\n{}".format(p))
        self.strategies = StrategyManager(self.subbed_topics, assumptions)

    def make_tests(self, config_num):
        monitors, tests = self._make_monitors_and_tests()
        for i in range(len(tests)):
            testable = tests[i]
            filename = "c{:03d}_test_{}.py".format(config_num, i+1)
            test_monitors = self._get_test_monitors(testable, monitors)
            self._write_test_files(tests[i], test_monitors, filename)
        if not tests:
            msg = "None of the given properties for {} is directly testable."
            msg = msg.format(self.config.name)
            self.iface.log_warning(msg)
            # TODO generate "empty" monitor, all others become secondary

    # FIXME fetching type tokens should be part of HAROS
    def _get_open_subbed_topics(self):
        ignored = self.settings.get("ignored", ())
        subbed = {} # topic -> TypeToken
        for topic in self.config.topics.enabled:
            if topic.subscribers and not topic.publishers:
                if topic.unresolved or topic.type == "?":
                    msg = "Skipping unresolved topic {} ({}).".format(
                        topic.rosname.full, self.config.name)
                    self.iface.log_warning(msg)
                elif topic.rosname.full in ignored:
                    msg = "Skipping ignored topic {} ({}).".format(
                        topic.rosname.full, self.config.name)
                    self.iface.log_warning(msg)
                else:
                    subbed[topic.rosname.full] = get_type(topic.type)
        return subbed

    # FIXME fetching type tokens should be part of HAROS
    def _get_all_pubbed_topics(self):
        ignored = self.settings.get("ignored", ())
        pubbed = {} # topic -> msg_type (TypeToken)
        for topic in self.config.topics.enabled:
            if topic.unresolved or topic.type == "?":
                msg = "Skipping unresolved topic {} ({}).".format(
                    topic.rosname.full, self.config.name)
                self.iface.log_warning(msg)
                continue
            if topic.publishers:
                if topic.rosname.full in ignored:
                    msg = "Skipping ignored topic {} ({}).".format(
                        topic.rosname.full, self.config.name)
                    self.iface.log_warning(msg)
                else:
                    pubbed[topic.rosname.full] = get_type(topic.type)
        return pubbed

    def _get_subscribers(self):
        subs = []
        for topic, type_token in self.subbed_topics.items():
            subs.append(Subscriber(topic, type_token, True))
        for topic, type_token in self.pubbed_topics.items():
            subs.append(Subscriber(topic, type_token, False))
        return subs

    def _make_monitors_and_tests(self):
        all_monitors = []
        tests = []
        for i in range(len(self.config.hpl_properties)):
            p = self.config.hpl_properties[i]
            if not p.is_fully_typed():
                self.iface.log_warning(
                    "Skipping untyped property:\n{}".format(p))
                continue
            uid = "P" + str(i + 1)
            monitor = MonitorTemplate(
                uid, p, self.pubbed_topics, self.subbed_topics)
            all_monitors.append(monitor)
            try:
                strategies = self.strategies.build_strategies(p)
            except StrategyError as e:
                self.iface.log_warning(
                    "Cannot produce a test for:\n'{}'\n\n{}".format(p, e))
            self._apply_slack(monitor)
            tests.append(TestTemplate(
                monitor, strategies, self._get_publishers(), self.subscribers,
                self.strategies.pkg_imports, monitor.hpl_string))
            monitor.variable_substitution()
        return all_monitors, tests

    def _get_test_monitors(self, test_case, monitors):
        extras = self.settings.get("extra_monitors", True)
        if extras is True:
            return monitors
        if extras is False:
            return (test_case.monitor,)
        if extras == "safety":
            ms = [m for m in monitors if m.is_safety]
            if test_case.monitor not in ms:
                ms.append(test_case.monitor)
            return ms
        # any other value is invalid; assume default behaviour
        return monitors

    def _get_publishers(self):
        # FIXME build list based on self.strategies stages
        pubs = []
        for topic, type_token in self.subbed_topics.items():
            rospy_type = type_token.type_name.replace("/", ".")
            pubs.append(PublisherTemplate(topic, type_token, rospy_type))
        return pubs

    def _length_for_field(self, field_token, type_token, lengths):
        selector = Selector(field_token, type_token)
        array_expr = None
        for i in range(len(selector.accessors)):
            f = selector.accessors[i]
            if f.ros_type.is_array and not f.ros_type.is_fixed_length:
                field = selector.subselect(i+1)
                array_expr = field.expression
            elif array_expr is not None:
                min_len = lengths.get(array_expr, 0)
                if f.is_range:
                    pass # TODO
                elif not f.is_dynamic:
                    min_len = max(min_len, int(f.field_name) + 1)
                    if min_len > 0:
                        lengths[array_expr] = min_len
                array_expr = None

    def _length_for_value(self, hpl_value, type_token, lengths, event_map):
        if hpl_value.is_literal:
            return
        if hpl_value.is_reference:
            if hpl_value.message is not None:
                type_token = event_map[hpl_value.message].type_token
            if hpl_value.token in type_token.constants:
                return
            self._length_for_field(hpl_value.token, type_token, lengths)
        elif hpl_value.is_range:
            self._length_for_value(hpl_value.lower_bound, type_token,
                                   lengths, event_map)
            self._length_for_value(hpl_value.upper_bound, type_token,
                                   lengths, event_map)
        elif hpl_value.is_set:
            for v in hpl_value.values:
                self._length_for_value(v, type_token, lengths, event_map)
        elif not hpl_value.is_variable:
            raise TypeError("unknown value type: " + type(hpl_value).__name__)

    def _apply_slack(self, monitor):
        slack = self.settings.get("slack", 0.0)
        if slack < 0.0:
            raise ValueError("slack time cannot be negative")
        for event in monitor.events:
            event.duration += slack
            event.log_age += slack
            if event.external_timer is not None:
                event.external_timer += slack

    def _write_test_files(self, test_case, all_monitors, filename, debug=False):
        # test_case: includes monitor for which traces will be generated
        # all_monitors: used for secondary monitors
        env = Environment(
            loader=PackageLoader(KEY, "templates"),
            line_statement_prefix=None,
            line_comment_prefix=None,
            trim_blocks=True,
            lstrip_blocks=True,
            autoescape=False
        )
        if debug:
            template = env.get_template("debug_monitor.python.jinja")
        else:
            template = env.get_template("test_script.python.jinja")
        data = {
            "events": tuple(e for m in all_monitors for e in m.events),
            "main_monitor": test_case.monitor,
            "monitors": all_monitors,
            "default_strategies": self.strategies.default_strategies,
            "custom_strategies": test_case.custom_strategies,
            "publishers": test_case.publishers,
            "subscribers": test_case.subscribers,
            "settings": self.settings,
            "log_level": "DEBUG",
            "pkg_imports": test_case.pkg_imports,
            "property_text": test_case.property_text,
            "slack": self.settings.get("slack", 0.0),
            "nodes": list(n.rosname.full for n in self.config.nodes.enabled),
            "commands": self.commands
        }
        with open(filename, "w") as f:
            f.write(template.render(**data).encode("utf-8"))
        mode = os.stat(filename).st_mode
        mode |= (mode & 0o444) >> 2
        os.chmod(filename, mode)
        self.iface.export_file(filename)


################################################################################
# Strategy Building
################################################################################

# stages: (topic -> strategy (random msg)) x3
# activator: strategy for the activator event
# trigger: strategy for the trigger event
Strategies = namedtuple("Strategies", ("stages", "activator", "trigger"))


# publisher stages:
#   - stage 1 [1+ chunks]: up to activator (if not launch)
#   - stage 2 [1+ chunks]: up to trigger
#   - stage 3 [0+ chunks]: after trigger (response and prevention)

# 'globally' and 'until' do not have stage 1
# only 'causes' and 'forbids' have stage 3
# only 'causes' and 'forbids' have trigger in stage 2

class StrategyManager(object):
    __slots__ = ("stage1", "stage2", "stage3")

    def __init__(self, pubs, assumptions):
        # pubs: topic -> ROS type token
        # assumptions: [HplAssumption]
        default_strategies = {}
        pkg_imports = {"std_msgs"}
        # topic -> (type token, predicate)
        topics = self._mapping(pubs, assumptions)
        self.stage1 = Stage1Builder(topics, default_strategies, pkg_imports)
        self.stage2 = Stage2Builder(topics, default_strategies, pkg_imports)
        self.stage3 = Stage3Builder(topics, default_strategies, pkg_imports)

    @property
    def default_strategies(self):
        assert (self.stage1.default_strategies is self.stage2.default_strategies
            and self.stage1.default_strategies is self.stage3.default_strategies)
        return self.stage1.default_strategies

    @property
    def pkg_imports(self):
        assert (self.stage1.pkg_imports is self.stage2.pkg_imports
            and self.stage1.pkg_imports is self.stage3.pkg_imports)
        return self.stage1.pkg_imports

    def build_strategies(self, prop):
        self.stage1.build(prop)
        self.stage2.build(prop)
        self.stage3.build(prop)
        randoms = (self.stage1.strategies, self.stage2.strategies,
                   self.stage3.strategies)
        return Strategies(randoms, self.stage1.activator, self.stage2.trigger)

    def _mapping(self, publishers, assumptions):
        r = {}
        for event in assumptions:
            topic = event.topic
            rostype = self.publishers.get(topic)
            if rostype is not None:
                r[topic] = (rostype, event.predicate)
        for topic, rostype in publishers.items():
            if topic not in r:
                r[topic] = (rostype, HplVacuousTruth())
        return r


class StrategyBuilder(object):
    __slots__ = ("types_by_message", "counter", "default_strategies",
                 "pkg_imports")

    def __init__(self, default_strategies, pkg_imports):
        self.types_by_message = {}
        self.counter = 0
        self.default_strategies = default_strategies
        self.pkg_imports = pkg_imports

    def _build(self, rostype, phi, fun_name="cms"):
        assert phi.is_predicate
        if phi.is_vacuous:
            if phi.is_true:
                return self._default_strategy(rostype)
            else:
                raise StrategyError("unsatisfiable predicate")
        conditions = self._convert_to_old_format(phi.condition)
        strategy = self._msg_generator(rostype, conditions)
        self.pkg_imports.add(rostype.package)
        self.counter += 1
        name = "{}{}_{}_{}".format(
            fun_name, self.counter, rostype.package, rostype.message)
        return MsgStrategy(name, strategy.args, type_token.package,
                           type_token.message, strategy.build(), False)

    def _default_strategy(self, rostype):
        assert rostype.is_message
        if rostype.type_name not in self.default_strategies:
            stack = [rostype]
            while stack:
                type_token = stack.pop()
                if type_token.is_primitive or type_token.is_header:
                    continue
                if type_token.is_time or type_token.is_duration:
                    continue
                if type_token.is_array or type_token.type_name in strategies:
                    continue
                self.pkg_imports.add(type_token.package)
                self.default_strategies[type_token.type_name] = type_token
                stack.extend(type_token.fields.values())
        return MsgStrategy(rostype.type_name.replace("/", "_"),
            (), rostype.package, rostype.message, (), True)

    def _convert_to_old_format(self, phi):
        # FIXME remove this and remake the strategy generator
        assert phi.is_expression and phi.can_be_bool
        relational = ("=", "!=", "<", "<=", ">", ">=", "in")
        conditions = []
        stack = [phi]
        while stack:
            expr = stack.pop()
            if expr.is_quantifier:
                raise StrategyError("quantifiers are not implemented")
            elif expr.is_function_call:
                raise StrategyError("function calls are not implemented")
            elif phi.is_accessor:
                expr = HplBinaryOperator("=", expr, HplLiteral("True", True))
                conditions.append(expr)
            elif expr.is_operator:
                if expr.arity == 1:
                    assert expr.operator == "not"
                    expr = expr.operand
                    if expr.is_accessor:
                        conditions.append(HplBinaryOperator("=", expr,
                            HplLiteral("False", False)))
                    elif expr.is_operator:
                        if expr.operator == "not":
                            stack.append(expr.operand)
                        elif expr.operator == "or":
                            stack.append(HplUnaryOperator("not", expr.operand1))
                            stack.append(HplUnaryOperator("not", expr.operand2))
                        elif expr.operator == "=":
                            stack.append(HplBinaryOperator("!=",
                                expr.operand1, expr.operand2))
                        elif expr.operator == "!=":
                            stack.append(HplBinaryOperator("=",
                                expr.operand1, expr.operand2))
                        elif expr.operator == "<":
                            stack.append(HplBinaryOperator(">=",
                                expr.operand1, expr.operand2))
                        elif expr.operator == "<=":
                            stack.append(HplBinaryOperator(">",
                                expr.operand1, expr.operand2))
                        elif expr.operator == ">":
                            stack.append(HplBinaryOperator("<=",
                                expr.operand1, expr.operand2))
                        elif expr.operator == ">=":
                            stack.append(HplBinaryOperator("<",
                                expr.operand1, expr.operand2))
                        elif expr.operator == "in":
                            x = expr.operand1
                            y = expr.operand2
                            if not x.is_accessor:
                                raise StrategyError(
                                    "general LHS operands are not implemented")
                            if not (y.is_accessor or y.is_value):
                                raise StrategyError(
                                    "general RHS operands are not implemented")
                            if y.is_accessor:
                                raise StrategyError(
                                    "general RHS operands are not implemented")
                            elif y.is_set:
                                for value in y.values:
                                    conditions.append(HplBinaryOperator("!=",
                                        x, value))
                            else:
                                assert y.is_range
                                raise StrategyError(
                                    "general RHS operands are not implemented")
                        else:
                            raise StrategyError("negation is not implemented")
                    else:
                        raise StrategyError("negation is not implemented")
                else:
                    if expr.operator == "and":
                        stack.append(expr.operand1)
                        stack.append(expr.operand2)
                    elif expr.operator in relational:
                        x = expr.operand1
                        y = expr.operand2
                        if not x.is_accessor:
                            raise StrategyError(
                                "general LHS operands are not implemented")
                        if not (y.is_accessor or y.is_value):
                            raise StrategyError(
                                "general RHS operands are not implemented")
                        conditions.append(expr)
                    else:
                        raise StrategyError("operators are not implemented")
        return conditions

    def _msg_generator(self, type_token, conditions):
        strategy = MessageStrategyGenerator(type_token)
        for condition in conditions:
            # FIXME Selector should accept AST nodes instead of strings
            selector = Selector(str(condition.operand1), type_token)
            strategy.ensure_generator(selector)
        for condition in conditions:
            self._set_condition(strategy, condition, type_token)
        return strategy

    def _set_condition(self, strategy, condition, type_token):
        selector = Selector(str(condition.operand1), type_token)
        value = self._value(condition.operand2, strategy, type_token)
        if condition.operator == "=":
            strategy.set_eq(selector, value)
        elif condition.operator == "!=":
            strategy.set_neq(selector, value)
        elif condition.operator == "<":
            strategy.set_lt(selector, value)
        elif condition.operator == "<=":
            strategy.set_lte(selector, value)
        elif condition.operator == ">":
            strategy.set_gt(selector, value)
        elif condition.operator == ">=":
            strategy.set_gte(selector, value)
        elif condition.operator == "in":
            if condition.operand2.is_range:
                if condition.operand2.exclude_min:
                    strategy.set_gt(selector, value[0])
                else:
                    strategy.set_gte(selector, value[0])
                if condition.operand2.exclude_max:
                    strategy.set_lt(selector, value[1])
                else:
                    strategy.set_lte(selector, value[1])
            else:
                strategy.set_in(selector, value)

    def _value(self, expr, strategy, type_token):
        if expr.is_accessor:
            msg = expr.base_message()
            if not msg.is_this_msg:
                assert msg.is_variable
                type_token = self.types_by_message[msg.name]
            # check for constants
            if expr.is_field and expr.message.is_value:
                ros_literal = type_token.constants.get(expr.field)
                if ros_literal is not None:
                    return ros_literal.value
            selector = Selector(str(expr), type_token)
            if msg.is_this_msg:
                return selector
            return strategy.make_msg_arg(msg.name, selector)
        assert expr.is_value
        if expr.is_literal:
            return expr.value
        if expr.is_range:
            return (self._value(expr.min_value, strategy, type_token),
                    self._value(expr.max_value, strategy, type_token))
        if expr.is_set:
            return tuple(self._value(v, strategy, type_token)
                         for v in expr.values)
        raise StrategyError("unknown value type: " + repr(expr))

"""
>>> from z3 import *
>>> x, y = BitVecs('msg.field @i', 32)
>>> s = Optimize()
>>> s.add(x + y == 10)
>>> s.add(UGT(x, 0x7FFFFFFF))
>>> mx = s.minimize(x)
>>> s.check()
sat
>>> s.model()
[@i = 2147483658, msg.field = 2147483648]
>>> s.model()[x].as_signed_long()
-2147483648

>>> s = Optimize()
>>> s.add(x + y == 10)
>>> s.add(ULE(x, 0x7FFFFFFF))
>>> mx = s.maximize(x)
>>> s.check()
sat
>>> s.model()[x].as_signed_long()
2147483647
>>> s.model()
[@i = 2147483659, msg.field = 2147483647]

32-bit float: x = FP('x', FPSort(8, 24))
64-bit float: x = FP('x', FPSort(11, 53))
"""


"""
>>> from sympy import Symbol, solve
>>> x = Symbol('x')
>>> r = solve(x**2 - 1, x)
>>> r
[-1, 1]
>>> type(r[0])
<class 'sympy.core.numbers.NegativeOne'>
>>> r = solve(x - 4, x)
>>> r = solve(x > 3, x)
>>> r
(3 < x) & (x < oo)
>>> type(r)
And
>>> r.is_number
False
>>> r.is_Boolean
True
>>> bool(r)
True
>>> r.args
(3 < x, x < oo)
>>> from sympy import Not, Eq
>>> solve(Not(Eq(x, 4)), x)
(x > -oo) & (x < oo) & Ne(x, 4)
>>> r = solve(Not(Eq(x, 4)), x)
>>> r.args
(x > -oo, x < oo, Ne(x, 4))
>>> r.is_number
False
>>> r.is_Boolean
True
>>> from sympy import Or
>>> r = solve(Or(x > 4, x < -4), x)
Traceback (most recent call last):
  File "<stdin>", line 1, in <module>
  File "/home/andre/ros/harosenv/local/lib/python2.7/site-packages/sympy/solvers/solvers.py", line 1174, in solve
    solution = _solve(f[0], *symbols, **flags)
  File "/home/andre/ros/harosenv/local/lib/python2.7/site-packages/sympy/solvers/solvers.py", line 1511, in _solve
    f_num, sol = solve_linear(f, symbols=symbols)
  File "/home/andre/ros/harosenv/local/lib/python2.7/site-packages/sympy/solvers/solvers.py", line 2085, in solve_linear
    eq = lhs - rhs
TypeError: unsupported operand type(s) for -: 'Or' and 'int'
>>> Or(solve(x > 4, x), solve(x < -4, x))
((-oo < x) & (x < -4)) | ((4 < x) & (x < oo))
>>> _
((-oo < x) & (x < -4)) | ((4 < x) & (x < oo))
>>> _.args
((-oo < x) & (x < -4), (4 < x) & (x < oo))
>>> r = solve([x * 2 + 3 > 15, x >= -128, x < 128], x)
>>> r
(6 < x) & (x < 128)
"""


class Stage1Builder(StrategyBuilder):
    __slots__ = StrategyBuilder.__slots__ + (
        "topics", "strategies", "activator")

    def __init__(self, topics, default_strategies, pkg_imports):
        super(Stage1Builder, self).__init__(default_strategies, pkg_imports)
        self.topics = topics

    def build(self, prop):
        self.strategies = {}
        self.activator = None
        event = prop.scope.activator
        if event is None:
            return # activator is launch; this stage does not exist
        self._build_activator(event)
        self._build_randoms(event)

    def _build_activator(self, event):
        topic = event.topic
        if topic not in self.topics:
            raise StrategyError("cannot publish on topic '{}'".format(topic))
        rostype, assumed = self.topics.get(topic)
        phi = event.predicate.join(assumed)
        self.activator = self._build(rostype, phi, fun_name="s1cs")

    def _build_randoms(self, event):
        for topic, data in self.topics.items():
            rostype, assumed = data
            if topic == event.topic: # activator topic
                phi = event.predicate
                if phi.is_vacuous:
                    continue # no random messages
                else:
                    # cannot match activator
                    phi = phi.negate().join(assumed)
                    self.strategies[topic] = self._build(
                        rostype, phi, fun_name="s1cs")
            else: # random topic
                self.strategies[topic] = self._build(
                    rostype, assumed, fun_name="s1cs")


class Stage2Builder(StrategyBuilder):
    __slots__ = StrategyBuilder.__slots__ + (
        "topics", "strategies", "trigger")

    def __init__(self, topics, default_strategies, pkg_imports):
        super(Stage2Builder, self).__init__(default_strategies, pkg_imports)
        self.topics = topics

    def build(self, prop):
        self.strategies = {}
        self.trigger = None
        trigger = prop.pattern.trigger
        terminator = prop.scope.terminator
        if prop.pattern.behaviour.topic in self.topics:
            raise StrategyError("topic '{}' is not advertised".format(topic))
        if prop.pattern.is_requirement:
            assert trigger is not None
            self._build_randoms(trigger, terminator)
        else:
            if prop.pattern.is_response and prop.pattern.is_prevention:
                assert trigger is not None
                activator = prop.scope.activator
                if activator is not None and activator.alias is not None:
                    rostype, assumed = self.topics.get(activator.topic)
                    self.types_by_message[activator.alias] = rostype
                self._build_trigger(trigger, terminator)
            self._build_randoms(None, terminator)

    def _build_trigger(self, trigger, terminator):
        topic = trigger.topic
        if topic not in self.topics:
            raise StrategyError("cannot publish on topic '{}'".format(topic))
        rostype, assumed = self.topics.get(topic)
        phi = trigger.predicate.join(assumed)
        if terminator is not None and terminator.topic == topic:
            if terminator.predicate.is_vacuous:
                raise StrategyError("trigger and terminator on the same topic")
            phi = phi.join(terminator.predicate.negate())
        self.trigger = self._build(rostype, phi, fun_name="s2cs")

    def _build_randoms(self, trigger, terminator):
        for topic, data in self.topics.items():
            rostype, assumed = data
            if trigger and topic == trigger.topic:
                phi = trigger.predicate
                if phi.is_vacuous:
                    continue # no random messages
                else: # cannot match trigger or terminator
                    phi = phi.negate()
                    if terminator and topic == terminator.topic:
                        psi = terminator.predicate
                        if psi.is_vacuous:
                            # TODO warning? trigger is impossible
                            continue # no random messages
                        else:
                            phi = phi.join(psi.negate())
                    phi = phi.join(assumed)
                    self.strategies[topic] = self._build(
                        rostype, phi, fun_name="s2cs")
            elif terminator and topic == terminator.topic:
                phi = terminator.predicate
                if phi.is_vacuous:
                    continue # no random messages
                else: # cannot match terminator
                    phi = phi.negate().join(assumed)
                    self.strategies[topic] = self._build(
                        rostype, phi, fun_name="s2cs")
            else: # random topic
                self.strategies[topic] = self._build(
                    rostype, assumed, fun_name="s2cs")


class Stage3Builder(StrategyBuilder):
    __slots__ = StrategyBuilder.__slots__ + ("topics", "strategies")

    def __init__(self, topics, default_strategies, pkg_imports):
        super(Stage3Builder, self).__init__(default_strategies, pkg_imports)
        self.topics = topics

    def build(self, prop):
        self.strategies = {}
        if not (prop.pattern.is_response or prop.pattern.is_prevention):
            return # other patterns do not have this stage
        self._build_randoms(prop.pattern.trigger, prop.scope.terminator)

    def _build_randoms(self, trigger, terminator):
        assert trigger is not None
        for topic, data in self.topics.items():
            rostype, assumed = data
            if topic == trigger.topic:
                phi = trigger.predicate
                if phi.is_vacuous:
                    continue # no random messages
                else: # cannot match trigger or terminator
                    phi = phi.negate()
                    if terminator and topic == terminator.topic:
                        psi = terminator.predicate
                        if psi.is_vacuous:
                            continue # no random messages
                        else:
                            phi = phi.join(psi.negate())
                    phi = phi.join(assumed)
                    self.strategies[topic] = self._build(
                        rostype, phi, fun_name="s3cs")
            elif terminator and topic == terminator.topic:
                phi = terminator.predicate
                if phi.is_vacuous:
                    continue # no random messages
                else: # cannot match terminator
                    phi = phi.negate().join(assumed)
                    self.strategies[topic] = self._build(
                        rostype, phi, fun_name="s3cs")
            else: # random topic
                self.strategies[topic] = self._build(
                    rostype, assumed, fun_name="s3cs")
