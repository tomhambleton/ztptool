#!/usr/bin/env python3
"""Convert an OpenROADM device JSON config (like input.json) into a sequence of
NETCONF <edit-config> messages, printed to stdout.

The input is the JSON encoding of an ``org-openroadm-device`` subtree (the
top-level key may be bare ``org-openroadm-device`` or the RFC 7951 form
``org-openroadm-device:org-openroadm-device``).

By default each top-level node becomes its own edit-config message: the ``info``
container is one message, and every entry of a top-level list (``shelves``,
``circuit-packs``, ``xponder`` ...) is its own message. Messages are emitted in
the input's document order, which for a well-formed device payload means parents
(e.g. circuit-pack ``1/3``) precede children (``1/3/1``) and the ``xponder`` that
references them comes last -- the order a device needs for provisioning.

Namespaces: the whole org-openroadm-device tree lives in
``http://org/openroadm/device``, so that namespace is declared once as the
default namespace on the <org-openroadm-device> root element and every child
inherits it -- exactly what the YANG model requires. If your input carries
augmentations from other modules as RFC 7951 ``module:node`` keys, add the
module->namespace mapping to MODULE_NS below and the converter will emit a fresh
xmlns at that boundary.
"""
import argparse
import json
import sys

from lxml import etree

DEVICE_NS = "http://org/openroadm/device"
NETCONF_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
ROOT_TAG = "org-openroadm-device"
EOM = "]]>]]>"  # NETCONF 1.0 end-of-message framing

# Maps RFC 7951 module prefixes (the "module:" part of a "module:node" JSON key)
# to XML namespaces. The device tree itself is wholly in DEVICE_NS, so the root
# declaration covers all of input.json; extend this for foreign augmentations.
MODULE_NS = {
    "org-openroadm-device": DEVICE_NS,
}

# Leaves that are "config false" (operational/inventory state) in the
# org-openroadm-device model and therefore rejected by an <edit-config>. These
# are filtered out by --config-only. The set was derived from the OpenROADM
# 13.1.1 device model; each name is config false wherever it appears in the
# device tree (info, shelves, circuit-packs, ports).
READ_ONLY_LEAVES = {
    "vendor", "model", "serial-id", "is-physical", "faceplate-label",
    "port-direction", "oamp-interface-name", "openroadm-version",
}

# Mandatory config leaves the device requires that a ZTP template may omit.
# --config-only injects these (using --admin-state) when absent. Keyed by the
# name of the list/container the entry belongs to.
MANDATORY_CONFIG = {
    "shelves": ("administrative-state",),
    "circuit-packs": ("administrative-state",),
}


def split_key(key):
    """Return (module_prefix_or_None, local_name) for a JSON member name."""
    if ":" in key:
        prefix, local = key.split(":", 1)
        return prefix, local
    return None, key


def to_text(value):
    """Render a scalar JSON value as YANG/XML leaf text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def build(parent, key, value, cur_ns):
    """Append element(s) for key/value under parent.

    A list produces one repeated element per entry (YANG list semantics). A
    default-namespace declaration is emitted only when the namespace changes,
    so children of org-openroadm-device do not repeat xmlns.
    """
    prefix, local = split_key(key)
    ns = MODULE_NS.get(prefix, cur_ns) if prefix else cur_ns

    if isinstance(value, list):
        for item in value:
            build(parent, key, item, cur_ns)
        return

    nsmap = {None: ns} if ns != cur_ns else None
    el = etree.SubElement(parent, "{%s}%s" % (ns, local), nsmap=nsmap)

    if isinstance(value, dict):
        for child_key, child_val in value.items():
            build(el, child_key, child_val, ns)
    elif value is not None:
        el.text = to_text(value)


def make_message(groups, msg_id, target, default_op):
    """Build one <rpc><edit-config> carrying the given (key, value) groups
    inside a single <org-openroadm-device> root element."""
    rpc = etree.Element("{%s}rpc" % NETCONF_NS, nsmap={None: NETCONF_NS})
    rpc.set("message-id", str(msg_id))

    edit = etree.SubElement(rpc, "{%s}edit-config" % NETCONF_NS)
    tgt = etree.SubElement(edit, "{%s}target" % NETCONF_NS)
    etree.SubElement(tgt, "{%s}%s" % (NETCONF_NS, target))
    if default_op:
        etree.SubElement(edit, "{%s}default-operation" % NETCONF_NS).text = default_op

    config = etree.SubElement(edit, "{%s}config" % NETCONF_NS)
    device = etree.SubElement(
        config, "{%s}%s" % (DEVICE_NS, ROOT_TAG), nsmap={None: DEVICE_NS}
    )
    for key, value in groups:
        build(device, key, value, DEVICE_NS)
    return rpc


def message_groups(device, single):
    """Yield lists of (key, value) groups, one list per output message.

    Default: one group per top-level node, splitting top-level lists per entry.
    With single=True: a single group containing the whole device subtree.
    """
    if single:
        yield list(device.items())
        return
    for key, value in device.items():
        if isinstance(value, list):
            for item in value:
                yield [(key, item)]
        else:
            yield [(key, value)]


def to_config_only(value, admin_state, node_name=None):
    """Return a copy of value with read-only leaves removed and mandatory config
    leaves injected, so the result is acceptable in an <edit-config>.

    node_name is the name of the list/container an entry belongs to, used to
    decide which mandatory leaves to inject.
    """
    if isinstance(value, dict):
        result = {
            key: to_config_only(val, admin_state, split_key(key)[1])
            for key, val in value.items()
            if split_key(key)[1] not in READ_ONLY_LEAVES
        }
        for leaf in MANDATORY_CONFIG.get(node_name, ()):
            result.setdefault(leaf, admin_state)
        return result
    if isinstance(value, list):
        return [to_config_only(item, admin_state, node_name) for item in value]
    return value


def unwrap(data):
    """Return the org-openroadm-device dict from a parsed JSON document."""
    if isinstance(data, dict) and len(data) == 1:
        (top_key, value), = data.items()
        _, local = split_key(top_key)
        if local == ROOT_TAG:
            return value
    # Fall back to treating the document itself as the device subtree.
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="JSON config file (e.g. input.json); use - for stdin")
    parser.add_argument("--target", default="candidate", choices=["candidate", "running"],
                        help="edit-config target datastore (default: candidate)")
    parser.add_argument("--default-operation", default="merge",
                        choices=["merge", "replace", "none"],
                        help="<default-operation> value (default: merge)")
    parser.add_argument("--single", action="store_true",
                        help="emit one edit-config for the whole subtree instead of one per node")
    parser.add_argument("--config-only", action="store_true",
                        help="drop read-only (config false) inventory leaves and inject mandatory "
                             "administrative-state so the output is acceptable in an edit-config")
    parser.add_argument("--admin-state", default="inService",
                        help="administrative-state value injected by --config-only (default: inService)")
    parser.add_argument("--start-message-id", type=int, default=101,
                        help="first message-id (default: 101)")
    parser.add_argument("--no-eom", action="store_true",
                        help="omit the ']]>]]>' NETCONF 1.0 message separator")
    args = parser.parse_args(argv)

    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    device = unwrap(json.loads(raw))
    if not isinstance(device, dict):
        parser.error("input does not contain an org-openroadm-device object")

    if args.config_only:
        device = to_config_only(device, args.admin_state)

    msg_id = args.start_message_id
    for groups in message_groups(device, args.single):
        rpc = make_message(groups, msg_id, args.target, args.default_operation)
        sys.stdout.write(etree.tostring(rpc, pretty_print=True, encoding="unicode"))
        if not args.no_eom:
            sys.stdout.write(EOM + "\n")
        msg_id += 1


if __name__ == "__main__":
    main()
