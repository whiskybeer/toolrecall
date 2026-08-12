#!/usr/bin/env python3
import sys
sys.path.insert(0, "bench")
import arms
print("agent loaded from:", agent.__file__)
import bench.hermes_arms as ha
ha.register_hermes_arms(agent)
fn = agent.make_agent_turn("hermes_compress")
print("hermes_compress arm:", "OK")
fn2 = agent.make_agent_turn("both")
print("both arm:", "OK")
fn3 = agent.make_agent_turn("prefix")
print("prefix arm:", "OK")
print("All 4 arms registered")