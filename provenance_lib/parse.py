from __future__ import annotations
import copy
from typing import Any, Iterable, Mapping, Optional, Set

import networkx as nx
from networkx.classes.reportviews import NodeView  # type: ignore

from . import checksum_validator
from . import zipfile_parser
from .zipfile_parser import Config, ParserResults, ProvNode, Parser
from .util import UUID


class ProvDAG:
    """
    A single-rooted DAG of UUIDs representing a single QIIME 2 Archive.


    ## DAG Attributes

    _parsed_artifact_uuids: Set[UUID] - the set of user-passed terminal node
        uuids. Used to generate properties like `terminal_uuids`, this is a
        superset of terminal_uuids.
    terminal_uuids: Set[UUID] - the set of terminal node ids present in the
        DAG, not including inner pipeline nodes.
    terminal_nodes: Set[ProvNode] - the terminal ProvNodes present in the DAG,
        not including inner pipeline nodes.
    provenance_is_valid: checksum_validator.ValidationCode - the canonical
        indicator of provenance validity, this contain the _poorest_
        ValidationCode from all parsed Artifacts unioned into a given ProvDAG.
    checksum_diff: checksum_validator.ChecksumDiff - a ChecksumDiff
        representing all added, removed, and changed filepaths from all parsed
        Artifacts. If an artifact's checksums.md5 file is missing, this may
        be None. When multiple artifacts are unioned, this field prefers
        ChecksumDiffs over Nonetypes, which will be dropped. For this reason,
        provenance_is_valid is a more reliable indicator of provenance validity
        thank checksum_diff.
    dag: nx.DiGraph - a Directed Acyclic Graph (DAG) representing the complete
        provenance of one or more QIIME 2 Artifacts. This DAG includes pipeline
        "alias" nodes, as well as the inner nodes that compose each pipeline.

    ## Methods/builtin suport
    `len`: int - ProvDAG supports the builtin len just as nx.DiGraph does,
        returning the number of nodes in `mydag.dag`
    nodes: networkx.classes.reportview.NodeView - A NodeView of self.dag
    relabel_nodes: Optional[ProvDAG]: provided with a mapping, relabels the
        nodes in self.dag. May be used inplace (returning None) or may return
        a relabeled copy of self
    union: ProvDAG - a class method that returns the union of many ProvDAGs

    ## GraphViews
    Graphviews are subgraphs of networkx graphs. They behave just like DiGraphs
    unless you take many views of views, at which point they lag.

    complete: `mydag.dag` is the DiGraph containing all recorded provenance
               nodes for this ProvDAG
    collapsed_view: `mydag.collapsed_view` returns a DiGraph (GraphView)
    containing a node for each standalone Action or Visualizer and one single
    node for each Pipeline (like q2view provenance trees)

    ## About the Nodes

    DiGraph nodes are literally UUIDs (strings)

    Every node has the following attributes:
    node_data: Optional[ProvNode]
    has_provenance: bool

    TODO: Now that we have outsourced the creation of ParserResults entirely,
    should ProvDAG vet that every node has node_data and has_provenance?
    Alternately, maybe we can enforce this in the Parser ABC.

    No-provenance nodes:
    When parsing v1+ archives, v0 ancestor nodes without tracked provenance
    (e.g. !no-provenance inputs) are discovered only as parents to the current
    inputs. They are added to the DAG when we add in-edges to "real" provenance
    nodes. These nodes are explicitly assigned the node attributes above,
    allowing red-flagging of no-provenance nodes, as all nodes have a
    has_provenance attribute. No-provenance nodes with no v1+ children will
    always appear as disconnected members of the DiGraph.

    Custom node objects:
    Though NetworkX supports the use of custom objects as nodes, querying the
    DAG for an individual graph node requires keying with object literals,
    which feels much less intuitive than with e.g. the UUID string of the
    ProvNode you want to access, and would make testing a bit clunky.
    """
    def __init__(self, artifact_data: Any, cfg: Config = Config()):
        """
        Create a ProvDAG (digraph) by getting a parser from the parser
        dispatcher, using it to parse the incoming data into a ParserResults,
        and then loading those Results into key fields.
        """
        dispatcher = ParserDispatcher(cfg, artifact_data)
        parser_results = dispatcher.parse(artifact_data)

        self._parsed_artifact_uuids = parser_results.parsed_artifact_uuids
        self.dag = parser_results.prov_digraph
        self._provenance_is_valid = parser_results.provenance_is_valid
        self._checksum_diff = parser_results.checksum_diff

        # clear cache whenever we create a new ProvDAG
        self._terminal_uuids = None  # type: Optional[Set[UUID]]

    def __repr__(self) -> str:
        return ('ProvDAG representing these Artifacts '
                f'{self._parsed_artifact_uuids}')

    __str__ = __repr__

    def __len__(self) -> int:
        return len(self.dag)

    # TODO: Is this a reasonable way to define dag equality?
    # We could also consider edges, but I think that may be overkill given the
    # use of UUIDs.
    def __eq__(self, other) -> bool:
        if (self.__class__ != other.__class__ or
            len(self.nodes) != len(other.nodes) or
                set(self.nodes) != set(other.nodes)):
            return False
        else:
            return True

    @property
    def terminal_uuids(self) -> Set[UUID]:
        """
        The UUID of the terminal node of one QIIME 2 Archive, generated by
        selecting all nodes in a collapsed view of self.dag with an out-degree
        of zero.

        We memoize the set of terminal UUIDs to prevent unnecessary traversals,
        so must set self._terminal_uuid back to None in any method that
        modifies the structure of self.dag, or the nodes themselves (which are
        literal UUIDs).

        These methods include at least union and relabel_nodes.
        """
        if self._terminal_uuids is not None:
            return self._terminal_uuids
        cv = self.collapsed_view
        self._terminal_uuids = {uuid for uuid, out_degree in cv.out_degree()
                                if out_degree == 0}
        return self._terminal_uuids

    @property
    def terminal_nodes(self) -> Set[ProvNode]:
        """The terminal ProvNode of one QIIME 2 Archive"""
        return {self.get_node_data(uuid) for uuid in self.terminal_uuids}

    @property
    def provenance_is_valid(self) -> checksum_validator.ValidationCode:
        return self._provenance_is_valid

    @property
    def checksum_diff(self) -> Optional[checksum_validator.ChecksumDiff]:
        return self._checksum_diff

    @property
    def nodes(self) -> NodeView:
        return self.dag.nodes

    @property
    def collapsed_view(self) -> nx.DiGraph:
        outer_nodes = set()
        for terminal_uuid in self._parsed_artifact_uuids:
            outer_nodes |= self.get_outer_provenance_nodes(terminal_uuid)

        def n_filter(node):
            return node in outer_nodes

        return nx.subgraph_view(self.dag, filter_node=n_filter)

    def has_edge(self, start_node: UUID, end_node: UUID) -> bool:
        """
        Returns True if the edge u, v is in the graph
        Calls nx.DiGraph.has_edge
        """
        return self.dag.has_edge(start_node, end_node)

    def node_has_provenance(self, uuid: UUID) -> bool:
        return self.dag.nodes[uuid]['has_provenance']

    def get_node_data(self, uuid: UUID) -> ProvNode:
        """Returns a ProvNode from this ProvDAG selected by UUID"""
        return self.dag.nodes[uuid]['node_data']

    def relabel_nodes(self, mapping: Mapping) -> None:
        """
        Helper method for safe use of nx.relabel.relabel_nodes, this updates
        the labels of self.dag in place.

        Also updates the DAG's _parsed_artifact_uuids to match the new labels,
        to head off KeyErrors downstream, and clears the _terminal_uuids cache.

        Users who need a copy of self.dag should use nx.relabel.relabel_nodes
        directly, and proceed at their own risk.

        TODO: 4th NEXT implement copy=True
        """
        nx.relabel_nodes(self.dag, mapping, copy=False)

        self._parsed_artifact_uuids = {mapping[uuid] for
                                       uuid in self._parsed_artifact_uuids}

        # Clear the _terminal_uuids cache so that property returns correctly
        self._terminal_uuids = None

    def union(self, others: Iterable[ProvDAG]) -> None:
        """
        Creates a new ProvDAG by unioning the graphs in an arbitrary number
        of ProvDAGs.

        Also updates the DAG's _parsed_artifact_uuids to include others' uuids,
        and clears the _terminal_uuids cache so we get complete results from
        that traversal.

        TODO: 5th NEXT rebuild this as a copy-only union, and update tests
        These params don't line up nicely with compose_all, which takes
        a list of graphs and always returns a new graph. Maybe this
        shouldn't expose a mutator - ony return provdags
        """
        dags = [self.dag]
        for other in others:
            dags.append(other.dag)
            self._parsed_artifact_uuids |= other._parsed_artifact_uuids
            self._provenance_is_valid = min(self.provenance_is_valid,
                                            other.provenance_is_valid)
            # Here we retain as much data as possible, preferencing
            # ChecksumDiffs over None. This might mean we keep a clean/empty
            # ChecksumDiff and drop None, used to indicate a missing
            # checksums.md5 file in a v5+ archive. _provenance_is_valid will
            # still be INVALID in this case.
            if other.checksum_diff is None:
                # Keep self.checksum_diff as it is
                continue

            if self.checksum_diff is None:
                self._checksum_diff = other.checksum_diff
            else:
                # Neither ChecksumDiff is None
                self.checksum_diff.added.update(other.checksum_diff.added)
                self.checksum_diff.removed.update(other.checksum_diff.removed)
                self.checksum_diff.changed.update(other.checksum_diff.changed)

        self.dag = nx.compose_all(dags)

        # Clear the _terminal_uuids cache so that property returns correctly
        self._terminal_uuids = None

    def get_outer_provenance_nodes(self, _node_id: UUID = None) -> Set[UUID]:
        """
        Selective depth-first traversal of this node_id's ancestors.
        Returns the set of "outer" nodes that represent "nested" provenance
        like that seen in q2view (i.e. all standalone Actions and Visualizers,
        and a single node for each Pipeline).

        Because the terminal/alias nodes created by pipelines show _pipeline_
        inputs, this recursion skips over all inner nodes.

        NOTE: _node_id exists to support recursive calls and may produce
        unexpected results if e.g. an "inner" node ID is passed.
        """
        nodes = set() if _node_id is None else {_node_id}
        parents = [edge_pair[0] for edge_pair in self.dag.in_edges(_node_id)]
        for uuid in parents:
            nodes = nodes | self.get_outer_provenance_nodes(uuid)
        return nodes


class ProvDAGParser(Parser):
    """
    Effectively a ProvDAG copy constructor, this "parses" a ProvDAG, loading
    its data into a new ProvDAG.
    """
    # Using strings here is kinda clumsy. Maybe fix that someday?
    accepted_data_types = 'ProvDAG'

    @classmethod
    def get_parser(cls, artifact_data: Any) -> Parser:
        if isinstance(artifact_data, ProvDAG):
            return ProvDAGParser()
        else:
            raise TypeError(
                f" in ProvDAGParser: {artifact_data} is not a ProvDAG")

    def parse_prov(self, cfg: Config, pdag: ProvDAG) -> ParserResults:
        return ParserResults(
            copy.deepcopy(pdag._parsed_artifact_uuids),
            copy.deepcopy(pdag.dag),
            copy.deepcopy(pdag.provenance_is_valid),
            copy.deepcopy(pdag.checksum_diff),
        )


class ParserDispatcher:
    """
    Parses VERSION file data, has a version-specific parser which allows
    for version-safe archive parsing
    """
    _PARSER_TYPE_REGISTRY = [
        zipfile_parser.ArtifactParser,
        ProvDAGParser
    ]

    accepted_data_types = [
        parser.accepted_data_types for parser in _PARSER_TYPE_REGISTRY]

    def __init__(self, cfg: Config, artifact_data: Any):
        self.cfg = cfg
        self.payload = artifact_data
        optional_parser = None
        errors = []
        for parser in self._PARSER_TYPE_REGISTRY:
            try:
                optional_parser = parser().get_parser(artifact_data)
                if optional_parser is not None:
                    self.parser = optional_parser  # type: Parser
                    break
            except Exception as e:
                errors.append(e)
        # If we finish the loop without a parser that can_handle, raise errors
        else:
            # Errors are only raised if no working parser is found,
            # so we can always raise unparseable_err if we raise errors.
            unparseable_err_msg = (
                        f"Input data {artifact_data} is not supported.\n"
                        "Parsers are available for the following data types: "
                        f"{self.accepted_data_types}")
            raise UnparseableDataError(unparseable_err_msg, errors)

    # TODO: Test that this appropriately handles different errors from one or
    # multiple Parser

    # TODO: Can we use mypy generics to make this Any more specific?
    def parse(self, artifact_data: Any) -> ParserResults:
        return self.parser.parse_prov(self.cfg, artifact_data)


class UnparseableDataError(Exception):
    """
    A specialized exception aggregator designed to deal more neatly with the
    fact that we may raise many different errors while attempting to identify
    a parser than can_handle our data.
    """

    def __init__(self, msg, aggregated_exceptions=None):
        self.message = msg
        self.exceptions = aggregated_exceptions

    def __repr__(self):
        msg = self.message + "\n"

        if self.exceptions is not None:
            msg += ("\nThe following errors were caught while trying to "
                    "identify a parser that can_handle this input data:")
        for e in self.exceptions:
            msg += ("\n- " + str(type(e))[7:-2] + str(e))

        return (msg)

    __str__ = __repr__
