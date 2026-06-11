#!/usr/bin/env python3
"""Convert an OpenROADM device JSON config (like input.json) into a sequence of
NETCONF <edit-config> messages.

By default the messages are printed to stdout. Given ``--host IP`` (with
``--username``), they are instead sent to that NETCONF server with ncclient,
one <edit-config> per message, printing each request sent and reply received;
on the first server or transport error the run stops and exits non-zero. When the candidate datastore is targeted (the
default) a commit is issued after all messages succeed (disable with
``--no-commit``).

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
import glob
import json
import os
import re
import sys
import tempfile
import time

from lxml import etree

DEVICE_NS = "http://org/openroadm/device"
NETCONF_NS = "urn:ietf:params:xml:ns:netconf:base:1.0"
ROOT_TAG = "org-openroadm-device"
EOM = "]]>]]>"  # NETCONF 1.0 end-of-message framing
TOKEN_RE = re.compile(r"__\w+")  # ZTP placeholder, e.g. __NODEID, __RACK_n
# A "module:identity" value, e.g. org-openroadm-interfaces:ethernetCsmacd
QNAME_RE = re.compile(r"^([A-Za-z_][\w.-]*):([A-Za-z_][\w.-]*)$")

# Module-name -> XML namespace. Both JSON keys (RFC 7951 "module:node" at a
# namespace boundary) and identityref leaf VALUES (RFC 7951 "module:identity")
# use the module name as the prefix; in XML each needs the module's namespace
# declared. This built-in set is extended at startup by scanning the YANG models
# (see load_module_namespaces / --models). The device tree itself is in
# DEVICE_NS, so plain device-namespace nodes need no per-element declaration.
MODULE_NS = {
    "org-openroadm-device": DEVICE_NS,
    "org-openroadm-interfaces": "http://org/openroadm/interfaces",
    "org-openroadm-routing": "http://org/openroadm/routing",
    "org-openroadm-ip": "http://org/openroadm/ip",
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


def load_module_namespaces(models_dir):
    """Scan *.yang under models_dir for `module <name> { namespace "<ns>"; }`
    and return a {module_name: namespace} map. Used to resolve the namespace of
    any module prefix that appears on a JSON key or identityref value."""
    name_re = re.compile(r"^\s*module\s+([\w.-]+)", re.M)
    ns_re = re.compile(r'^\s*namespace\s+"([^"]+)"', re.M)
    result = {}
    for path in glob.glob(os.path.join(models_dir, "**", "*.yang"), recursive=True):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        name = name_re.search(text)
        ns = ns_re.search(text)
        if name and ns:
            result[name.group(1)] = ns.group(1)
    return result


def split_key(key):
    """Return (module_prefix_or_None, local_name) for a JSON member name."""
    if ":" in key:
        prefix, local = key.split(":", 1)
        return prefix, local
    return None, key


def qualify_identityref(value, schema_node, element_ns, resolver):
    """Return (leaf_text, nsmap_additions) for an identityref leaf value.

    An identityref value is a YANG identity that must be XML-namespace-qualified
    when it is defined outside the element's own namespace. Two cases:

    * "module:identity" (already prefixed, e.g. org-openroadm-interfaces:gcc):
      declare that module's namespace and keep the value. Recognised without a
      schema; the known-module check avoids misreading colon-bearing strings
      such as IPv6 addresses.
    * a bare identity (e.g. R100G) on a leaf the schema says is identityref:
      look up the module that defines the identity and, if it differs from the
      element's namespace, prefix the value and declare that namespace.
    """
    text = to_text(value)
    match = QNAME_RE.match(text)
    if match and match.group(1) in MODULE_NS:
        return text, {match.group(1): MODULE_NS[match.group(1)]}
    if resolver is not None and ":" not in text and resolver.is_identityref(schema_node):
        info = resolver.identity_namespace(text)
        if info:
            module, namespace = info
            if namespace != element_ns:
                return "%s:%s" % (module, text), {module: namespace}
    return text, {}


def to_text(value):
    """Render a scalar JSON value as YANG/XML leaf text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# --- schema-driven element namespacing ------------------------------------
#
# Augmented containers (e.g. interface/ipv4 from org-openroadm-ip, interface/och
# from org-openroadm-optical-channel-interfaces, the top-level routing tree) live
# in their augmenting module's namespace, but the templates use bare JSON keys
# that don't carry that namespace. The same local name can even belong to
# different namespaces by position (interface/ipv4 is org-openroadm-ip while
# static-routes/ipv4 is org-openroadm-ipv4-unicast-routing), so the only correct
# resolution is to walk the YANG schema in parallel with the JSON.
#
# SchemaResolver loads a targeted slice of the model (org-openroadm-device plus
# the modules that augment it) with yangson, dropping any module yangson refuses
# (e.g. one with an unresolvable leafref) until the data model builds. If yangson
# or the models are unavailable it degrades to None and the converter falls back
# to namespace inheritance.

_MODULE_NAME_RE = re.compile(r"org-openroadm-[\w-]+")


def _index_models(dirs):
    """Index *.yang under dirs: {module_name: metadata} plus available revisions.
    Prefers the unversioned file (latest revision) for each module."""
    rev_re = re.compile(r'^\s*revision\s+"?(\d{4}-\d{2}-\d{2})"?', re.M)
    ns_re = re.compile(r'^\s*namespace\s+"([^"]+)"', re.M)
    type_re = re.compile(r"^\s*(module|submodule)\s+[\w.-]+", re.M)
    belongs_re = re.compile(r"^\s*belongs-to\s+([\w.-]+)", re.M)
    feature_re = re.compile(r"^\s*feature\s+([\w.-]+)", re.M)
    import_re = re.compile(r"\b(?:import|include)\s+([\w.-]+)\s*\{([^}]*)\}", re.S)
    revdate_re = re.compile(r'revision-date\s+"?(\d{4}-\d{2}-\d{2})"?')

    unversioned, revs = {}, {}
    for directory in dirs:
        if not os.path.isdir(directory):
            continue
        for fname in os.listdir(directory):
            if not fname.endswith(".yang"):
                continue
            base = fname[:-5]
            if "@" in base:
                name, rev = base.split("@", 1)
                revs.setdefault(name, set()).add(rev)
            else:
                unversioned.setdefault(base, os.path.join(directory, fname))

    meta = {}
    for name, path in unversioned.items():
        text = open(path, encoding="utf-8", errors="replace").read()
        type_m = type_re.search(text)
        ns_m = ns_re.search(text)
        belongs_m = belongs_re.search(text)
        found_revs = rev_re.findall(text)
        deps = [(dep, (revdate_re.search(body).group(1) if revdate_re.search(body) else None))
                for dep, body in import_re.findall(text)]
        meta[name] = {
            "rev": found_revs[0] if found_revs else None,   # first listed = latest
            "ns": ns_m.group(1) if ns_m else None,
            "kind": type_m.group(1) if type_m else "module",
            "belongs": belongs_m.group(1) if belongs_m else None,
            "features": sorted(set(feature_re.findall(text))),
            "deps": deps,
        }
    return {"meta": meta, "revs": revs}


def _load_identities(dirs, name_to_ns):
    """Map identity name -> (module_name, namespace) by scanning `identity X`
    statements. Lets a bare identityref value be qualified with the namespace of
    the module that defines it."""
    name_re = re.compile(r"^\s*module\s+([\w.-]+)", re.M)
    identity_re = re.compile(r"^\s*identity\s+([\w.-]+)", re.M)
    result = {}
    for directory in dirs:
        for path in glob.glob(os.path.join(directory, "*.yang")):
            if "@" in os.path.basename(path):
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
            mod = name_re.search(text)
            if not mod:
                continue
            module = mod.group(1)
            ns = name_to_ns.get(module)
            if not ns:
                continue
            for ident in identity_re.findall(text):
                result.setdefault(ident, (module, ns))
    return result


def _device_augmenting_modules(device_dir, common_dir):
    """Names of modules that augment the org-openroadm-device tree, plus the
    device module itself. These are the candidates whose namespaces matter."""
    augment_re = re.compile(r'augment\s+"/org-openroadm-device:')
    name_re = re.compile(r"^\s*module\s+([\w.-]+)", re.M)
    names = {"org-openroadm-device"}
    for directory in (device_dir, common_dir):
        for path in glob.glob(os.path.join(directory, "*.yang")):
            if "@" in os.path.basename(path):
                continue
            text = open(path, encoding="utf-8", errors="replace").read()
            if augment_re.search(text):
                m = name_re.search(text)
                if m:
                    names.add(m.group(1))
    return sorted(names)


def _yang_library(seed, excluded, index):
    """Build an ietf-yang-library for the import closure of seed (minus excluded),
    keeping only modules whose required revisions are all available as files."""
    meta, revs = index["meta"], index["revs"]

    def deps(name):
        return meta.get(name, {}).get("deps", ())

    def rev_ok(dep, want):
        if dep not in meta:
            return False
        return want is None or want == meta[dep]["rev"] or want in revs.get(dep, ())

    seen, stack = set(), [s for s in seed if s not in excluded]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(dep for dep, _ in deps(name))

    bad = set(excluded) | {n for n in seen if n not in meta}
    changed = True
    while changed:
        changed = False
        for name in list(seen - bad):
            if any(dep in bad or not rev_ok(dep, want) for dep, want in deps(name)):
                bad.add(name)
                changed = True
    keep = seen - bad

    chosen = {n: meta[n]["rev"] for n in keep}
    for name in keep:
        for dep, want in deps(name):
            if want and dep in keep:
                chosen[dep] = want  # honor an explicitly imported revision-date

    modules = []
    for name in sorted(keep):
        if meta[name]["kind"] == "submodule":
            continue
        entry = {"name": name, "conformance-type": "implement"}
        if chosen.get(name):
            entry["revision"] = chosen[name]
        if meta[name]["ns"]:
            entry["namespace"] = meta[name]["ns"]
        if meta[name]["features"]:
            entry["feature"] = meta[name]["features"]
        submodules = [
            {"name": sn, **({"revision": meta[sn]["rev"]} if meta[sn]["rev"] else {})}
            for sn in sorted(keep)
            if meta[sn]["kind"] == "submodule" and meta[sn]["belongs"] == name
        ]
        if submodules:
            entry["submodule"] = submodules
        modules.append(entry)
    return {"ietf-yang-library:modules-state":
            {"module-set-id": "ztp-targeted", "module": modules}}


class SchemaResolver:
    def __init__(self, datamodel, name_to_ns, identities, identityref_type):
        self._dm = datamodel
        self._name_to_ns = name_to_ns
        self._identities = identities          # identity name -> (module_name, namespace)
        self._identityref_type = identityref_type
        self.root = datamodel.get_data_node("/org-openroadm-device:org-openroadm-device")

    def child(self, schema_node, local_name):
        """Return the schema node for local_name under schema_node, or None."""
        if schema_node is None or not hasattr(schema_node, "data_children"):
            return None
        for candidate in schema_node.data_children():
            if candidate.name == local_name:
                return candidate
        return None

    def namespace(self, schema_node):
        """XML namespace URI for a schema node (yangson reports the module name)."""
        return self._name_to_ns.get(schema_node.ns) if schema_node is not None else None

    def is_identityref(self, schema_node):
        """True if schema_node is a leaf/leaf-list whose type is identityref."""
        return isinstance(getattr(schema_node, "type", None), self._identityref_type)

    def identity_namespace(self, name):
        """(module_name, namespace) of the module that defines identity name, or None."""
        return self._identities.get(name)

    @classmethod
    def load(cls, models_dir, warn=None):
        """Build a resolver from models_dir, or return None if unavailable."""
        try:
            from yangson import DataModel
            from yangson.datatype import IdentityrefType
        except ImportError:
            if warn:
                warn("yangson not installed; skipping schema-driven element namespacing")
            return None
        device = os.path.join(models_dir, "Device")
        common = os.path.join(models_dir, "Common")
        ietf = os.path.join(os.path.dirname(models_dir.rstrip(os.sep)), "ietf")
        if not (os.path.isdir(device) and os.path.isdir(common)):
            if warn:
                warn("models dir %s has no Device/Common; skipping schema namespacing" % models_dir)
            return None
        name_to_ns = load_module_namespaces(models_dir)
        identities = _load_identities([device, common], name_to_ns)
        index = _index_models([device, common, ietf])
        seed = _device_augmenting_modules(device, common)
        excluded = set()
        for _ in range(40):
            library = _yang_library(seed, excluded, index)
            fd, tmp = tempfile.mkstemp(suffix=".json", prefix="ztp-yang-library-")
            try:
                with os.fdopen(fd, "w") as fh:
                    json.dump(library, fh)
                dm = DataModel.from_file(tmp, mod_path=(device, common, ietf))
                return cls(dm, name_to_ns, identities, IdentityrefType)
            except Exception as exc:  # noqa: BLE001 - drop the offending module and retry
                match = _MODULE_NAME_RE.search(str(exc.args[0]) if exc.args else str(exc))
                if not match or match.group(0) in excluded:
                    if warn:
                        warn("could not build schema for namespacing: %s" % exc)
                    return None
                excluded.add(match.group(0))
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        return None


def build(parent, key, value, cur_ns, schema_node=None, resolver=None):
    """Append element(s) for key/value under parent.

    A list produces one repeated element per entry (YANG list semantics). A
    default-namespace declaration is emitted only when the namespace changes,
    so children of org-openroadm-device do not repeat xmlns. When a resolver and
    schema_node are supplied, the element's namespace is taken from the YANG
    schema, so augmented nodes (e.g. interface/ipv4) get their module namespace.
    """
    prefix, local = split_key(key)
    ns = MODULE_NS.get(prefix, cur_ns) if prefix else cur_ns

    child_schema = None
    if resolver is not None and not prefix:
        child_schema = resolver.child(schema_node, local)
        schema_ns = resolver.namespace(child_schema) if child_schema is not None else None
        if schema_ns:
            ns = schema_ns

    if isinstance(value, list):
        for item in value:
            build(parent, key, item, cur_ns, schema_node, resolver)
        return

    nsmap = {}
    if ns != cur_ns:
        nsmap[None] = ns
    leaf_text = None
    if not isinstance(value, dict) and value is not None:
        # Namespace-qualify an identityref value (prefixed or bare) so its
        # identity is bound rather than left dangling or wrongly inherited.
        leaf_text, value_ns = qualify_identityref(value, child_schema, ns, resolver)
        nsmap.update(value_ns)
    el = etree.SubElement(parent, "{%s}%s" % (ns, local), nsmap=nsmap or None)

    if isinstance(value, dict):
        for child_key, child_val in value.items():
            build(el, child_key, child_val, ns, child_schema, resolver)
    elif value is not None:
        el.text = leaf_text


def build_config(groups, resolver=None):
    """Build a <config> element (NETCONF base namespace) holding the given
    (key, value) groups inside a single <org-openroadm-device> root element.
    This is the payload for a NETCONF <edit-config>."""
    config = etree.Element("{%s}config" % NETCONF_NS, nsmap={None: NETCONF_NS})
    device = etree.SubElement(
        config, "{%s}%s" % (DEVICE_NS, ROOT_TAG), nsmap={None: DEVICE_NS}
    )
    device_schema = resolver.root if resolver is not None else None
    for key, value in groups:
        build(device, key, value, DEVICE_NS, device_schema, resolver)
    return config


def make_message(groups, msg_id, target, default_op, resolver=None):
    """Build one <rpc><edit-config> carrying the given groups (for stdout)."""
    rpc = etree.Element("{%s}rpc" % NETCONF_NS, nsmap={None: NETCONF_NS})
    rpc.set("message-id", str(msg_id))

    edit = etree.SubElement(rpc, "{%s}edit-config" % NETCONF_NS)
    tgt = etree.SubElement(edit, "{%s}target" % NETCONF_NS)
    etree.SubElement(tgt, "{%s}%s" % (NETCONF_NS, target))
    if default_op:
        etree.SubElement(edit, "{%s}default-operation" % NETCONF_NS).text = default_op
    edit.append(build_config(groups, resolver))
    return rpc


def _pretty_xml(xml_text):
    """Pretty-print an XML string for display; return it unchanged if unparsable."""
    try:
        return etree.tostring(etree.fromstring(xml_text.encode()),
                              pretty_print=True, encoding="unicode")
    except Exception:  # noqa: BLE001 - show raw on any parse issue
        return xml_text if xml_text.endswith("\n") else xml_text + "\n"


def _show(banner, xml_text):
    """Print a NETCONF message to stdout under a banner."""
    sys.stdout.write("===== %s =====\n" % banner)
    sys.stdout.write(_pretty_xml(xml_text))
    sys.stdout.flush()


def send_messages(groups_list, args, resolver):
    """Send each group as a NETCONF <edit-config> via ncclient, printing each
    request sent and reply received, stopping on the first error. Commits
    afterwards when targeting the candidate datastore."""
    try:
        from ncclient import manager
        from ncclient.operations import RPCError
    except ImportError:
        sys.exit("error: ncclient is required to send to a NETCONF server "
                 "(pip install ncclient)")

    try:
        session = manager.connect(
            host=args.host, port=args.port,
            username=args.username, password=args.password,
            key_filename=args.ssh_key,
            hostkey_verify=args.hostkey_verify,
            allow_agent=True, look_for_keys=args.ssh_key is None,
            timeout=args.timeout,
            device_params={"name": "default"},
        )
    except Exception as exc:  # noqa: BLE001 - connection/auth failure
        sys.exit("error: cannot connect to %s:%d: %s" % (args.host, args.port, exc))

    total = len(groups_list)
    sent = 0
    try:
        for index, groups in enumerate(groups_list, start=1):
            if index > 1 and args.delay > 0:
                time.sleep(args.delay)
            config = build_config(groups, resolver)
            payload = etree.tostring(config, encoding="unicode")
            request = make_message(groups, index, args.target, args.default_operation, resolver)
            _show("SENT edit-config %d/%d" % (index, total),
                  etree.tostring(request, encoding="unicode"))
            try:
                reply = session.edit_config(target=args.target, config=payload,
                                            default_operation=args.default_operation)
            except RPCError as exc:
                _show("RECEIVED edit-config %d/%d (rpc-error)" % (index, total),
                      getattr(exc, "xml", None) or str(exc))
                sys.exit(1)
            except Exception as exc:  # noqa: BLE001 - transport/other failure
                sys.stderr.write("error: edit-config %d/%d failed: %s\n" % (index, total, exc))
                sys.exit(1)
            _show("RECEIVED edit-config %d/%d" % (index, total), reply.xml)
            sent += 1

        if args.target == "candidate" and not args.no_commit:
            _show("SENT commit", "<commit/>")
            try:
                reply = session.commit()
            except Exception as exc:  # noqa: BLE001 - commit failure
                _show("RECEIVED commit (error)", getattr(exc, "xml", None) or str(exc))
                sys.exit(1)
            _show("RECEIVED commit", reply.xml)
    finally:
        try:
            session.close_session()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
    sys.stderr.write("done: %d edit-config message(s) applied to %s\n" % (sent, args.target))


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


def substitute(value, values):
    """Return a copy of value with every ZTP token replaced from the values map.

    Replacement is done within string leaf values (handles both whole-value and
    embedded tokens). Longer tokens are applied first so a token that is a prefix
    of another (e.g. __RACK vs __RACK_n) cannot corrupt the longer one.
    """
    if isinstance(value, dict):
        return {key: substitute(val, values) for key, val in value.items()}
    if isinstance(value, list):
        return [substitute(item, values) for item in value]
    if isinstance(value, str):
        for token in sorted(values, key=len, reverse=True):
            if token in value:
                value = value.replace(token, values[token])
    return value


def unsubstituted_tokens(value):
    """Return the set of __TOKEN placeholders still present in string values."""
    found = set()
    if isinstance(value, dict):
        for val in value.values():
            found |= unsubstituted_tokens(val)
    elif isinstance(value, list):
        for item in value:
            found |= unsubstituted_tokens(item)
    elif isinstance(value, str):
        found.update(TOKEN_RE.findall(value))
    return found


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
    parser.add_argument("--values", metavar="FILE",
                        help="JSON map of ZTP placeholders to site values (e.g. values.json); "
                             "tokens like __NODEID are substituted before conversion")
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
    parser.add_argument("--models", metavar="DIR",
                        default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                             "models", "openroadm-13.1.1"),
                        help="YANG models dir used for module->namespace mappings and for "
                             "schema-driven element namespacing "
                             "(default: models/openroadm-13.1.1 next to this script)")
    parser.add_argument("--no-schema-ns", action="store_true",
                        help="disable schema-driven namespacing of augmented elements "
                             "(e.g. interface/ipv4, routing); falls back to namespace inheritance")

    netconf = parser.add_argument_group(
        "NETCONF server (when --host is given, edit-configs are sent via ncclient "
        "instead of printed to stdout)")
    netconf.add_argument("--host", metavar="IP",
                         help="NETCONF server address; enables sending via ncclient")
    netconf.add_argument("--port", type=int, default=830,
                         help="NETCONF server port (default: 830)")
    netconf.add_argument("--username", help="NETCONF username")
    netconf.add_argument("--password", help="NETCONF password (else SSH key/agent is used)")
    netconf.add_argument("--ssh-key", metavar="FILE", help="SSH private key file for authentication")
    netconf.add_argument("--timeout", type=int, default=30,
                         help="NETCONF operation timeout in seconds (default: 30)")
    netconf.add_argument("--hostkey-verify", action="store_true",
                         help="verify the server SSH host key (default: off)")
    netconf.add_argument("--no-commit", action="store_true",
                         help="do not commit after a successful candidate-datastore run")
    netconf.add_argument("--delay", type=float, default=1.0, metavar="SECONDS",
                         help="delay between sending messages (default: 1.0; 0 to disable)")
    args = parser.parse_args(argv)

    if args.host and not args.username:
        parser.error("--username is required when --host is given")

    # Resolve module->namespace from the models so identityref values like
    # org-openroadm-interfaces:ethernetCsmacd get the right xmlns in XML.
    if args.models and os.path.isdir(args.models):
        MODULE_NS.update(load_module_namespaces(args.models))

    # Build the schema resolver so augmented elements get their module namespace.
    resolver = None
    if not args.no_schema_ns and args.models and os.path.isdir(args.models):
        resolver = SchemaResolver.load(
            args.models, warn=lambda m: sys.stderr.write("warning: %s\n" % m))

    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    data = json.loads(raw)

    if args.values:
        with open(args.values, encoding="utf-8") as fh:
            values = json.load(fh)
        if not isinstance(values, dict) or not all(isinstance(v, str) for v in values.values()):
            parser.error("--values file must be a JSON object mapping tokens to string values")
        data = substitute(data, values)
        leftover = unsubstituted_tokens(data)
        if leftover:
            sys.stderr.write("warning: tokens not found in %s, left unsubstituted: %s\n"
                             % (args.values, ", ".join(sorted(leftover))))

    device = unwrap(data)
    if not isinstance(device, dict):
        parser.error("input does not contain an org-openroadm-device object")

    if args.config_only:
        device = to_config_only(device, args.admin_state)

    groups_list = list(message_groups(device, args.single))

    if args.host:
        send_messages(groups_list, args, resolver)
        return

    msg_id = args.start_message_id
    for groups in groups_list:
        rpc = make_message(groups, msg_id, args.target, args.default_operation, resolver)
        sys.stdout.write(etree.tostring(rpc, pretty_print=True, encoding="unicode"))
        if not args.no_eom:
            sys.stdout.write(EOM + "\n")
        msg_id += 1


if __name__ == "__main__":
    main()
