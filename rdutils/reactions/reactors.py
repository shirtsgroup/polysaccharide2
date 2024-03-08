'''Classes for implementing reactions with respect to some set of reactant RDMols'''

from typing import ClassVar, Generator, Iterable, Optional, Union
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from itertools import combinations, chain

from rdkit import Chem
from rdkit.Chem import rdqueries

from .reactions import AnnotatedReaction, RxnProductInfo
from .. import rdprops
from ..labeling import bondwise, molwise
from ..rdtypes import RDMol

from ...monomers.specification import SANITIZE_AS_KEKULE


# CUSTOM QUERIES FOR ATOMS MODIFIED DURING A RXN
dummy_prop_query = rdqueries.HasPropQueryAtom('was_dummy') # heavy atom which was converted from a dummy atom in a reaction
HEAVY_FORMER_LINKER_QUERY = Chem.MolFromSmarts('A')
HEAVY_FORMER_LINKER_QUERY.GetAtomWithIdx(0).ExpandQuery(dummy_prop_query) # cast as Mol to allow for quick check via GetSubstructMatch


# REACTOR BASE CLASS 
@dataclass
class Reactor:
    '''Class for executing a reaction template on collections of RDMol "reactants"'''
    rxn_schema : AnnotatedReaction

    _has_reacted : bool = field(init=False, default=False)
    _ridx_prop_name : ClassVar[str] = field(init=False, default='reactant_idx') # name of the property to assign reactant indices to; set for entire class

    # PRE-REACTION PREPARATION METHODS
    def _activate_reaction(self) -> None:
        '''Check that the reaction schema provided is well defined and initialized'''
        pass

    def __post_init__(self) -> None:
        '''Pre-processing of reaction and reactant Mols'''
        self._activate_reaction()

    @classmethod
    def _label_reactants(cls, reactants : Iterable[RDMol]) -> None:
        '''Assigns "reactant_idx" Prop to all reactants to help track where atoms go during the reaction'''
        for i, reactant in enumerate(reactants):
            for atom in reactant.GetAtoms():
                atom.SetIntProp(cls._ridx_prop_name, i)

    # POST-REACTION CLEANUP METHODS
    @classmethod
    def _relabel_reacted_atoms(cls, product : RDMol, reactant_map_nums : dict[int, int]) -> None:
        '''Re-assigns "reactant_idx" Prop to modified reacted atoms to re-complete atom-to-reactant numbering'''
        for atom_id in rdprops.atom_ids_with_prop(product, 'old_mapno'):
            atom = product.GetAtomWithIdx(atom_id)
            map_num = atom.GetIntProp('old_mapno')

            atom.SetIntProp(cls._ridx_prop_name, reactant_map_nums[map_num])
            atom.SetAtomMapNum(map_num) # TOSELF : in future, might remove this (makes mapping significantly easier, but is ugly for labelling)

    @staticmethod
    def _sanitize_bond_orders(product : RDMol, product_template : RDMol, product_info : RxnProductInfo) -> None:
        '''Ensure bond order changes specified by the reaction are honored by RDKit'''
        for prod_bond_id, map_num_pair in product_info.mod_bond_ids_to_map_nums.items():
            target_bond = product_template.GetBondWithIdx(prod_bond_id)

            product_bond = bondwise.get_bond_by_map_num_pair(product, map_num_pair)
            # product_bond = product.GetBondBetweenAtoms(*rdlabels.atom_ids_by_map_nums(product, *map_num_pair))
            assert(product_bond.GetBeginAtom().HasProp('_ReactionDegreeChanged')) 
            assert(product_bond.GetEndAtom().HasProp('_ReactionDegreeChanged')) # double check that the reaction agrees that the bond has changed

            product_bond.SetBondType(target_bond.GetBondType()) # set bond type to what it *should* be from the reaction schema

    # REACTION EXECUTION METHODS
    def react(self, reactants : Iterable[RDMol], repetitions : int=1, clear_props : bool=False) -> list[RDMol]:
        '''Execute reaction over a collection of reactants and generate product molecule(s)'''
        self._label_reactants(reactants) # assign reactant indices in-place
        raw_products = self.rxn_schema.RunReactants(reactants, maxProducts=repetitions) # obtain unfiltered RDKit reaction output. TODO : generalize to work when more than 1 repetition is requested
        
        # post-reaction cleanup
        products = []
        for i, product in enumerate(chain.from_iterable(raw_products)): # clean up products into a usable form
            self._relabel_reacted_atoms(product, self.rxn_schema.map_nums_to_reactant_nums)
            self._sanitize_bond_orders(product,
                product_template=self.rxn_schema.GetProductTemplate(i),
                product_info=self.rxn_schema.product_info_maps[i]
            )
            if clear_props:
                rdprops.clear_atom_props(product, in_place=True)

            products.append(product)
        self._has_reacted = True # set reaction flag
        
        return products


# REACTOR SUBCLASSES
## SPECIAL CASES WITH SPECIFIC NUMBERS OF REACTANTS/PRODUCTS
@dataclass
class AdditionReactor(Reactor):
    '''Special case of Reactor with two reactant species forming one product'''
    def __post_init__(self) -> None:
        assert(self.rxn_schema.GetNumReactantTemplates() == 2)
        assert(self.rxn_schema.GetNumProductTemplates() == 1)

        return super().__post_init__()
    
    @property
    def product_info(self) -> RDMol:
        return self.rxn_schema.product_info_maps[0]

    def react(self, reactants : Iterable[RDMol], repetitions : int = 1, clear_props : bool = False) -> Optional[RDMol]:
        products = super().react(reactants, repetitions, clear_props) # return first (and only) product as standalone molecule
        if products:
            return products[0]

@dataclass
class CondensationReactor(Reactor):
    '''Special case of Reactor with two reactant species forming one product plus a small-molecule side product'''
    pass # TODO : implement behavior here

## POLYMERIZATION
class NoIntermonomerBondsFound(Exception):
    '''To be raised when search for newly-formed inter-monoer bonds fail'''
    pass

class IntermonomerBondIdentificationStrategy(ABC):
    '''Abstract base for Intermonomer Bond Identification Strategies for fragmentation during in-silico polymerization'''
    @abstractmethod
    def locate_intermonomer_bonds(self, product : RDMol, product_info : RxnProductInfo) -> Generator[int, None, None]:
        '''Generates the indices of all identified inter-monomer bonds by molecule'''
        pass

    def produce_fragments(self, product : RDMol, product_info : RxnProductInfo, separate : bool=True):
        '''Apply break all bonds identified by this IBIS algorithm and return the resulting fragments'''
        fragments = Chem.FragmentOnBonds(
            mol=product,
            bondIndices=self.locate_intermonomer_bonds(product, product_info) # TODO : check that the multiplicity of any bond to cut is no greater than the bond order
        ) # TODO : add config for "dummyLabels" arg to support port flavor setting
        if separate:
            return Chem.GetMolFrags(fragments, asMols=True)
        return fragments # if separation is not requested, return as single unfragmented molecule object
IBIS = IntermonomerBondIdentificationStrategy # shorthand alias for convenience

class ReseparateRGroups(IBIS):
    '''IBIS which cleaves any new bonds formed between atoms that were formerly the start of an R-group in the reaction template'''
    def locate_intermonomer_bonds(self, product: RDMol,product_info : RxnProductInfo) -> Generator[int, None, None]:
        possible_bridgehead_ids = [atom_id for match in product.GetSubstructMatches(HEAVY_FORMER_LINKER_QUERY) for atom_id in match]
        for new_bond_id in product_info.new_bond_ids_to_map_nums.keys():                     # for each newly formed bond...
            print(new_bond_id)
            for bridgehead_id_pair in combinations(possible_bridgehead_ids, 2):                   # ...find the most direct path between bridgehead atoms...
                print('\t', bridgehead_id_pair)
                if new_bond_id in bondwise.get_shortest_path_bonds(product, *bridgehead_id_pair): # ...and check if the new bond lies along it
                    yield new_bond_id

@dataclass
class PolymerizationReactor(AdditionReactor):
    '''Reactor which handles monomer partitioning post-polymerization condensation reaction'''
    def _inter_monomer_bond_candidates(self, product : RDMol) -> Generator[int, None, None]:
        '''Returns the bond indices of the most likely candidate for a newly-formed bond # TODO : expand this to 
        between heavy atoms in a product formed by the reat() method of this Reactor'''
        # determine indices of former linkers (i.e. outside of monomers) which are now heavy atoms (i.e. non-hydrogen) 
        possible_bridgehead_ids = [atom_id for match in product.GetSubstructMatches(HEAVY_FORMER_LINKER_QUERY) for atom_id in match]
        for new_bond_id in self.product_info.new_bond_ids_to_map_nums.keys():                     # for each newly formed bond...
            for bridgehead_id_pair in combinations(possible_bridgehead_ids, 2):                   # ...find the most direct path between bridgehead atoms...
                if new_bond_id in bondwise.get_shortest_path_bonds(product, *bridgehead_id_pair): # ...and check if the new bond lies along it
                    yield new_bond_id

    def polymerized_fragments(self, product : RDMol, separate : bool=True) -> Union[RDMol, tuple[RDMol]]:
        '''Cut product on inter-monomer bond, returning the resulting fragments'''
        try:
            inter_monomer_bond_idx = next(self._inter_monomer_bond_candidates(product)) # take first candidate bond index as intermonomer bond
        except StopIteration:
            raise NoIntermonomerBondsFound
        
        fragments = Chem.FragmentOnBonds(
            molwise.clear_atom_map_nums(product, in_place=False), # fragment unmapped copy of product for clarity 
            bondIndices=[inter_monomer_bond_idx] 
        )

        if separate:
            return Chem.GetMolFrags(fragments, asMols=True)
        return fragments # if separation is not requested, return as single unfragmented molecule object
    
    def propagate(self, monomers : Iterable[RDMol]) -> Generator[tuple[RDMol, tuple[RDMol]], None, None]:
        '''Keep reacting and fragmenting a pair of monomers until all reactive sites have been reacted
        Returns fragment pairs at each step of the chain propagation process'''
        reactants = monomers # initialize reactive pair with monomers
        while True:
            dimer = self.react(reactants, repetitions=1, clear_props=False) # can't clear properties here, otherwise fragment finding won't work
            if not dimer: # stop propagating once monomers can no longer react
                break
            # implicit "else"
            Chem.SanitizeMol(dimer, sanitizeOps=SANITIZE_AS_KEKULE)
            reactants = self.polymerized_fragments(dimer, separate=True)
            
            yield dimer, reactants # yield the dimerized fragment and the 2 new reactive fragments