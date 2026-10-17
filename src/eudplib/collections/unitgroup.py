# Copyright 2022 by Armoha.
# All rights reserved.
# This file is part of EUD python library (eudplib),
# and is released under "MIT License Agreement". Please see the LICENSE
# file that should have been included as part of this package.

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Literal, overload

from ..core import (
    Action,
    Add,
    AtLeast,
    ConstExpr,
    Deaths,
    DeathsX,
    EUDVariable,
    EUDVArray,
    EUDXVariable,
    Exactly,
    Forward,
    IsEUDVariable,
    Memory,
    NextTrigger,
    PopTriggerScope,
    PushTriggerScope,
    RawTrigger,
    SeqCompute,
    SetDeathsX,
    SetMemory,
    SetNextPtr,
    SetNextTrigger,
    SetTo,
    Subtract,
    VProc,
)
from ..core.eudstruct.vararray import _InternalVArray
from ..ctrlstru import DoActions, EUDEndWhile, EUDSetContinuePoint
from ..ctrlstru.loopblock import _unsafe_whilenot
from ..localize import _
from ..memio.modcurpl import f_setcurpl2cpcache
from ..scdata import CurrentPlayer
from ..utils import EPD, EPError, EUDPeekBlock, ep_assert, unProxy

_assign_helper = EUDVariable()


class _GroupField:
    def __init__(self, capacity, kind, default):
        self.kind = kind
        self.default = default
        self.values = EUDVArray(capacity)([default] * capacity)

    def read(self, index):
        value = self.values[index]
        return self.kind.cast(value) if self.kind is not None else value

    def write(self, index, value):
        raw = unProxy(value)
        if self.kind is not None and not (isinstance(raw, int) and raw == 0):
            value = self.kind.cast(value)
        self.values[index] = value

    def copy(self, source, destination):
        # Moving stored values does not construct or cast a static reference.
        self.values[destination] = self.values[source]


class UnitGroup:
    """Loop-efficient CUnit EPD collection

    ```js
    // epScript example

    // UnitGroup Declaration
    const zerglings = UnitGroup(1000);
    // max capacity = 1000, will use CPTrick

    // Register Unit
    zerglings.add(epd);

    // Loop UnitGroup
    foreach(unit : zerglings.cploop) {
        // Run Triggers on **any** zerglings (alive or dead)
        foreach(dead : unit.dying) {
            // Run Triggers on dead zerglings
        }  // <- dead zergling will be removed at end of *dying* block
        // Run Triggers on alive zerglings
    }

    // example usage
    function afterTriggerExec() {
        const zerglings = UnitGroup(1000);
        foreach(cunit : EUDLoopNewCUnit()) {
            epdswitch(cunit + 0x64/4, 255) {
            case $U("Zerg Zergling"):
                zerglings.add(epd);
                break;
            }
        }
        foreach(unit : zerglings.cploop) {
            foreach(dead : unit.dying) {
                // spawn Infested Terran when zergling dies
                dead.move_cp(0x4C / 4);  // Owner
                const owner = bread_cp(0, 0);
                dead.move_cp(0x28 / 4);  // Unit Position
                const x, y = posread_cp(0);

                setloc("loc", x, y);
                CreateUnit(1, "Infested Terran", "loc", owner);
            }
        }
    }
    ```
    """

    _fields_: Sequence[str | tuple] = ()

    def __init__(self, capacity: int, *, fields: Sequence[str | tuple] | None = None) -> None:
        self._extra_fields = {}
        schema = self._fields_ if fields is None else fields
        for spec in schema:
            kind, default = None, 0
            if isinstance(spec, str):
                name = spec
            else:
                ep_assert(isinstance(spec, tuple) and 2 <= len(spec) <= 3,
                          'UnitGroup fields must be names, (name, default), or (name, type, default)')
                name = spec[0]
                if len(spec) == 3:
                    kind, default = spec[1:]
                elif hasattr(spec[1], 'cast'):
                    kind = spec[1]
                else:
                    default = spec[1]
            ep_assert(isinstance(name, str) and name.isidentifier() and not name.startswith('_'),
                      f'Invalid UnitGroup field name: {name}')
            ep_assert(not hasattr(_CpHelper, name) and name not in ('offset', 'get_epd', 'dying'),
                      f'UnitGroup field conflicts with entry member: {name}')
            ep_assert(name not in self._extra_fields, f'Duplicate UnitGroup field: {name}')
            ep_assert(kind is None or callable(getattr(kind, 'cast', None)),
                      f'Invalid type for UnitGroup field: {name}')
            raw = unProxy(default)
            ep_assert(isinstance(raw, (int, ConstExpr)),
                      f'UnitGroup field defaults must be compile-time constants: {name}')
            if kind is not None and not (isinstance(raw, int) and raw == 0):
                default = kind.cast(default)
            self._extra_fields[name] = _GroupField(capacity, kind, default)
        self.capacity = capacity
        self.loopvar = EUDXVariable(EPD(0x6509B0), SetTo, 0)
        varray = _InternalVArray(capacity)(
            dest=self.loopvar, nextptr=self.loopvar.GetVTable()
        )
        self.varray = varray
        self.trg = EUDVariable(self.varray + 72 * capacity)
        self.pos = EUDVariable(EPD(self.varray) + 87 + 18 * capacity)
        self.cploop = _CPLoop(self)
        self._length = EUDVariable(0)

    @property  # FIXME: eudplib code can mutate length
    def length(self):
        return self._length

    def add(self, unit_epd, *field_values, **field_kwargs) -> None:
        fields = self._extra_fields
        ep_assert(len(field_values) <= len(fields), 'Too many UnitGroup field values')
        names = tuple(fields)
        initial = dict(zip(names, field_values))
        for name, value in field_kwargs.items():
            ep_assert(name in fields, f'Unknown UnitGroup field: {name}')
            ep_assert(name not in initial, f'Duplicate UnitGroup field value: {name}')
            initial[name] = value
        if IsEUDVariable(unit_epd):
            # Occupy 2T, Run 4T 10A
            SeqCompute(
                [
                    (self._length, Add, 1),
                    (self.trg, Add, -72),
                    (self.pos, Add, -18),
                    (unit_epd, Add, 0x4C // 4),
                    (EPD(unit_epd._varact) + 6, SetTo, 0x072D0000),
                    (EPD(unit_epd.getDestAddr()), None, self.pos),
                    (None, None, unit_epd),
                ]
            )

            SeqCompute([(unit_epd, Add, -(0x4C // 4))])
        else:
            # Occupy 1T, Run 4T 9A
            global _assign_helper
            SeqCompute(
                [
                    (self._length, Add, 1),
                    (self.trg, Add, -72),
                    (self.pos, Add, -18),
                    (_assign_helper, SetTo, unit_epd + 0x4C // 4),
                    (EPD(_assign_helper.getDestAddr()), None, self.pos),
                    (None, None, _assign_helper),
                ]
            )

        if fields:
            slot = self.capacity - self._length
            for name, field in fields.items():
                field.write(slot, initial.get(name, field.default))


class _CPLoop:
    def __init__(self, parent: UnitGroup) -> None:
        self._parent = parent

    def __iter__(self) -> Iterator[_CpHelper]:
        loopstart, pos, check_death = Forward(), Forward(), Forward()
        trg, tpos, loopvar, varray, capacity = (
            self._parent.trg,
            self._parent.pos,
            self._parent.loopvar,
            self._parent.varray,
            self._parent.capacity,
        )
        VProc(
            [trg, tpos],
            [
                trg.SetDest(EPD(loopstart)),
                tpos.SetDest(EPD(pos)),
                SetNextPtr(loopvar.GetVTable(), check_death),
            ],
        )
        fields = self._parent._extra_fields
        index = capacity - self._parent._length if fields else None

        def reset_cp() -> None:
            PushTriggerScope()
            reset_nextptr = RawTrigger(
                actions=SetNextPtr(loopvar.GetVTable(), check_death)
            )
            PopTriggerScope()
            nextptr = Forward()
            RawTrigger(
                nextptr=loopvar.GetVTable(),
                actions=[
                    SetNextPtr(loopvar.GetVTable(), reset_nextptr),
                    SetNextPtr(reset_nextptr, nextptr),
                ],
            )
            nextptr << NextTrigger()

        if _unsafe_whilenot()(Memory(loopstart, AtLeast, varray + 72 * capacity)):
            block = EUDPeekBlock("whileblock")[1]
            PushTriggerScope()  # remove entry
            remove_fields = NextTrigger() if fields else None
            if fields:
                source = capacity - self._parent._length
                for field in fields.values():
                    field.copy(source, index)
            remove_end = Forward()
            remove_start = RawTrigger(
                nextptr=trg.GetVTable(),
                actions=[
                    trg.SetDest(EPD(trg.GetVTable()) + 1),
                    SetNextPtr(loopvar.GetVTable(), remove_end),
                    loopvar.SetDest(0),  # pos가 이 액션의 값 칸
                ],
            )
            pos << remove_start + 328 + 32 * 2 + 20
            remove_end << RawTrigger(
                actions=[
                    self._parent._length.SubtractNumber(1),
                    trg.AddNumber(72),
                    tpos.AddNumber(18),
                    loopvar.SetDest(EPD(0x6509B0)),
                    SetNextPtr(loopvar.GetVTable(), check_death),
                ]
            )
            SetNextTrigger(block["contpoint"])
            PopTriggerScope()

            loopstart << block["loopstart"] + 4
            check_death << NextTrigger()

            def get_epd() -> EUDVariable:
                ret = EUDVariable()
                VProc(loopvar, loopvar.SetDest(ret))
                DoActions(
                    SetNextPtr(loopvar.GetVTable(), check_death),
                    loopvar.SetDest(EPD(0x6509B0)),
                    ret.SubtractNumber(19),
                )
                return ret

            # EUDIf()(DeathsX(CurrentPlayer, Exactly, 0, 0, 0xFF00)):
            yield _CpHelper(0x4C // 4, reset_cp, remove_fields if fields else remove_start,
                            get_epd, fields, index)
            if not block["contpoint"].IsSet():
                EUDSetContinuePoint()
            DoActions(SetMemory(loopstart, Add, 72), SetMemory(pos, Add, 18))
            if fields:
                DoActions(index.AddNumber(1))
        EUDEndWhile()

        f_setcurpl2cpcache()
        ep_assert(pos.IsSet(), "unit.dying must be added")


# TODO: Add CUnit-like convenient methods
class _CpHelper:
    def __init__(
        self,
        offset,
        resetf: Callable[[], None],
        remove: RawTrigger,
        epdf: Callable[[], EUDVariable],
        fields=None,
        index=None,
    ) -> None:
        self.offset = offset
        self._reset = resetf
        self._remove_start = remove
        self.get_epd = epdf
        self.dying = _Dying(self)
        self._in_dying_block = False
        self._fields = fields or {}
        self._index = index

    def __getattr__(self, name):
        fields = self.__dict__.get('_fields', {})
        if name not in fields:
            raise AttributeError(name)
        return fields[name].read(self._index)

    def __setattr__(self, name, value):
        fields = self.__dict__.get('_fields', {})
        if name in fields:
            fields[name].write(self._index, value)
        else:
            object.__setattr__(self, name, value)

    def remove(self) -> None:
        if self._in_dying_block:
            e = _("unit.remove() is automatically called at end of unit.dying block")
            raise EPError(e)
        SetNextTrigger(self._remove_start)

    @property  # read-only
    def epd(self) -> EUDVariable:
        return self.get_epd()

    @overload
    def set_cp(self, offset, *, action: Literal[False] = False) -> None:
        ...

    @overload
    def set_cp(self, offset, *, action: Literal[True]) -> Action:
        ...

    def set_cp(self, offset, *, action: bool = False) -> Action | None:
        self._reset()
        self.offset = 19
        return self.move_cp(offset, action=False)

    @overload
    def move_cp(self, offset, *, action: Literal[False] = False) -> None:
        ...

    @overload
    def move_cp(self, offset, *, action: Literal[True]) -> Action:
        ...

    def move_cp(self, offset, *, action: bool = False) -> Action | None:
        mod, val = None, None
        try:
            if offset > self.offset:
                mod, val = Add, offset - self.offset
            elif offset < self.offset:
                mod, val = Subtract, self.offset - offset
        except TypeError as e:
            if isinstance(offset, ConstExpr) and isinstance(self.offset, int):
                mod, val = Add, offset - self.offset
            elif isinstance(offset, int) and isinstance(self.offset, ConstExpr):
                mod, val = Subtract, self.offset - offset
            else:
                raise e
        self.offset = offset
        if mod and val:
            if action:
                return SetMemory(0x6509B0, mod, val)
            else:
                DoActions(SetMemory(0x6509B0, mod, val))
        return None


class _Dying:
    def __init__(self, helper: _CpHelper) -> None:
        self._helper: _CpHelper = helper

    def __iter__(self) -> Iterator[_CpHelper]:
        self._helper._in_dying_block = True
        dying_end, check_death, dying_block = Forward(), Forward(), Forward()
        RawTrigger(
            actions=[
                self._helper.move_cp(0x08 // 4, action=True),
                SetNextPtr(check_death, dying_end),
            ]
        )
        RawTrigger(
            conditions=Deaths(CurrentPlayer, Exactly, 0, 0),
            actions=[
                self._helper.move_cp(0x4C // 4, action=True),
                SetDeathsX(CurrentPlayer, SetTo, 0, 0, 0xFF00),
                self._helper.move_cp(0x08 // 4, action=True),
            ],
        )
        self._helper.move_cp(0x4C // 4)
        check_death << RawTrigger(
            nextptr=dying_end,
            conditions=DeathsX(CurrentPlayer, Exactly, 0, 0, 0xFF00),
            actions=SetNextPtr(check_death, dying_block),
        )
        dying_block << NextTrigger()
        yield self._helper
        self._helper._in_dying_block = False
        self._helper.remove()
        self._helper.offset = 19
        dying_end << NextTrigger()
