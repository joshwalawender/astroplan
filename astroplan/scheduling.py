# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""
Tools for scheduling observations.
"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import copy
from abc import ABCMeta, abstractmethod

import numpy as np

from astropy import units as u

__all__ = ['ObservingBlock', 'TransitionBlock', 'ImageSet',
           'Scheduler', 'SequentialScheduler', 'SummingScheduler',
           'Transitioner']


class ImageSet(object):
    """
    An object that represents a set of images taken with the same instrument
    configuration (e.g. filter and exposure time).

    Parameters
    ----------
    duration : `Quantity` with time units
        The estimated duration of the ImageSet.
    configuration : dict
        Dictionary describing the configuration of the instrument.
    """
    @u.quantity_input(duration=u.second)
    def __init__(self, duration, configuration={}):
        self.duration = duration
        self.configuration = configuration

    @classmethod
    @u.quantity_input(overhead=u.second)
    def from_exposures(cls, timeperexp, nexp, overhead=0*u.second,
                            configuration={}):
        duration = nexp*(timeperexp + overhead)
        imset = cls(duration, configuration=configuration)
        return imset

class ObservingBlock(object):
    @u.quantity_input(duration=u.second)
    def __init__(self, target, duration, constraints=None, priority=1.0):
        self.target = target
        self.duration = duration
        self.constraints = constraints
        self.priority = priority
        self.start_time = self.end_time = None

    def __repr__(self):
        orig_repr = object.__repr__(self)
        if self.start_time is None or self.end_time is None:
            return orig_repr.replace('object at', '({0}, unscheduled) at'.format(self.target.name))
        else:
            s = '({0}, {1} to {2}) at'.format(self.target.name, self.start_time, self.end_time)
            return orig_repr.replace('object at', s)

    @classmethod
    def from_imagesets(cls, target, imageset_list, priority=1.0):
        duration = 0
        for IS in imageset_list:
            duration += IS.duration
        ob = cls(target, duration, priority=priority)
        return ob

class TransitionBlock(object):
    """
    An object that represents "dead time" between observations, while the
    telescope is slewing, instrument is reconfiguring, etc.

    Parameters
    ----------
    components : dict
        A dictionary mapping the reason for an observation's dead time to
        `Quantity`s with time units
    start_time : Quantity with time units

    """
    def __init__(self, components, start_time=None):
        self.start_time = start_time
        self.components = components

    def __repr__(self):
        orig_repr = object.__repr__(self)
        comp_info = ', '.join(['{0}: {1}'.format(c, t) 
                               for c, t in self.components.items()])
        if self.start_time is None or self.end_time is None:
            return orig_repr.replace('object at', ' ({0}, unscheduled) at'.format(comp_info))
        else:
            s = '({0}, {1} to {2}) at'.format(comp_info, self.start_time, self.end_time)
            return orig_repr.replace('object at', s)

    @property
    def end_time(self):
        return self.start_time + self.duration

    @property
    def components(self):
        return self._components
    @components.setter
    def components(self, val):
        duration = 0*u.second
        for t in val.values():
            duration += t

        self._components = val
        self.duration = duration


class Scheduler(object):
    __metaclass__ = ABCMeta

    def __call__(self, blocks):
        """
        Schedule a set of `ObservingBlock`s

        Parameters
        ----------
        blocks : iterable of `ObservingBlock`s
            The blocks to schedule.  Note that these blocks will *not*
            be modified - new ones will be created and returned.

        Returns
        -------
        schedule : list
            A list of `ObservingBlock`s and `TransitionBlock`s with populated
            `start_time` and `end_time` attributes
        """
        #these are *shallow* copies
        copied_blocks = [copy.copy(block) for block in blocks]
        new_blocks, already_sorted = self._make_schedule(copied_blocks)
        if not already_sorted:
            block_time_map = {block.start_time : block for block in new_blocks}
            new_blocks = [block_time_map[time] for time in sorted(block_time_map)]
        return new_blocks

    @abstractmethod
    def _make_schedule(self, blocks):
        """
        Does the actual business of scheduling. The `blocks` passed in should
        have their `start_time` and `end_time` modified to reflect the schedule.
        any necessary `TransitionBlock` should also be added.  Then the full set
        of blocks should be returned as a list of blocks, along with a boolean
        indicating whether or not they have been put in order already.

        Parameters
        ----------
        blocks : list of `ObservingBlock`s
            Can be modified as it is already copied by `__call__`

        Returns
        -------
        new_blocks : list of blocks
            The blocks from ``blocks``, as well as any necessary
            `TransitionBlock`s
        already_sorted : bool
            If True, the ``new_blocks`` come out pre-sorted, otherwise they need
            to be sorted.
        """
        raise NotImplementedError
        return new_blocks, already_sorted

class SequentialScheduler(Scheduler):
    """
    A scheduler that does "stupid simple sequential scheduling".  That is, it
    simply looks at all the blocks, picks the best one, schedules it, and then
    moves on.

    Parameters
    ----------
    start_time : `~astropy.time.Time`
        the start of the observation scheduling window.
    end_time : `~astropy.time.Time`
        the end of the observation scheduling window.
    constraints : sequence of `Constraint`s
        The constraints to apply to *every* observing block.  Note that
        constraints for specific blocks can go on each block individually.
    observer : `astroplan.Observer`
        The observer/site to do the scheduling for.
    transitioner : `Transitioner` or None
        The object to use for computing transition times between blocks
    gap_time : `Quantity` with time units
        The minimal spacing to try over a gap where nothing can be scheduled.

    """
    @u.quantity_input(gap_time=u.second)
    def __init__(self, start_time, end_time, constraints, observer,
                       transitioner=None, gap_time=30*u.min):
        self.constraints = constraints
        self.start_time = start_time
        self.end_time = end_time
        self.observer = observer
        self.transitioner = transitioner
        self.gap_time = gap_time

    @classmethod
    @u.quantity_input(duration=u.second)
    def from_timespan(cls, center_time, duration, **kwargs):
        """
        Create a new instance of this class given a time and
        """
        start_time = center_time - duration/2.
        end_time = center_time + duration/2.
        return cls(start_time, end_time, **kwargs)

    def _make_schedule(self, blocks):
        for b in blocks:
            if b.constraints is None:
                b._all_constraints = self.constraints
            else:
                b._all_constraints = self.constraints + b.constraints
            b._duration_offsets = u.Quantity([0*u.second, b.duration/2, b.duration])


        new_blocks = []
        current_time = self.start_time
        while (len(blocks) > 0) and (current_time < self.end_time):

            # first compute the value of all the constraints for each block
            # given the current starting time
            block_transitions = []
            block_constraint_results = []
            for b in blocks:
                #first figure out the transition
                if len(new_blocks) > 0:
                    trans = self.transitioner(new_blocks[-1], b, current_time, self.observer)
                else:
                    trans = None
                block_transitions.append(trans)
                transition_time = 0*u.second if trans is None else trans.duration

                times = current_time + transition_time + b._duration_offsets

                constraint_res = []
                for constraint in b._all_constraints:
                    constraint_res.append(constraint(self.observer, [b.target], times))
                # take the product over all the constraints *and* times
                block_constraint_results.append(np.prod(constraint_res))

            # now identify the block that's the best
            bestblock_idx = np.argmax(block_constraint_results)

            if block_constraint_results[bestblock_idx] == 0.:
                # if even the best is unobservable, we need a gap
                new_blocks.append(TransitionBlock({'nothing_observable': self.gap_time}, current_time))
                current_time += self.gap_time
            else:
                # If there's a best one that's observable, first get its transition
                trans = block_transitions.pop(bestblock_idx)
                if trans is not None:
                    new_blocks.append(trans)
                    current_time += trans.duration

                # now assign the block itself times and add it to the schedule
                newb = blocks.pop(bestblock_idx)
                newb.start_time = current_time
                current_time += self.gap_time
                newb.end_time = current_time
                newb.constraints_value = block_constraint_results[bestblock_idx]

                new_blocks.append(newb)

        return new_blocks, True


class SummingScheduler(Scheduler):
    """
    A scheduler that does "stupid simple sequential scheduling" using a weighted
    sum of the constraint results.
    
    Parameters
    ----------
    start_time : `~astropy.time.Time`
        the start of the observation scheduling window.
    end_time : `~astropy.time.Time`
        the end of the observation scheduling window.
    constraints : sequence of `Constraint`s
        The constraints to apply to *every* observing block.  Note that
        constraints for specific blocks can go on each block individually.
    observer : `astroplan.Observer`
        The observer/site to do the scheduling for.
    transitioner : `Transitioner` or None
        The object to use for computing transition times between blocks
    gap_time : `Quantity` with time units
        The minimal spacing to try over a gap where nothing can be scheduled.

    """
    @u.quantity_input(gap_time=u.second)
    def __init__(self, start_time, end_time, constraints, observer,
                       transitioner=None, gap_time=30*u.min):
        self.constraints = constraints
        self.start_time = start_time
        self.end_time = end_time
        self.observer = observer
        self.transitioner = transitioner
        self.gap_time = gap_time

    @classmethod
    @u.quantity_input(duration=u.second)
    def from_timespan(cls, center_time, duration, **kwargs):
        """
        Create a new instance of this class given a time and
        """
        start_time = center_time - duration/2.
        end_time = center_time + duration/2.
        return cls(start_time, end_time, **kwargs)

    def _make_schedule(self, blocks):
        for b in blocks:
            if b.constraints is None:
                b._all_constraints = self.constraints
            else:
                b._all_constraints = self.constraints + b.constraints
            b._duration_offsets = u.Quantity([0*u.second, b.duration/2, b.duration])


        new_blocks = []
        current_time = self.start_time
        while (len(blocks) > 0) and (current_time < self.end_time):

            # first compute the value of all the constraints for each block
            # given the current starting time
            block_transitions = []
            block_constraint_results = []
            for b in blocks:
                vetoed = False
                #first figure out the transition
                if len(new_blocks) > 0:
                    trans = self.transitioner(new_blocks[-1], b, current_time, self.observer)
                else:
                    trans = None
                block_transitions.append(trans)
                transition_time = 0*u.second if trans is None else trans.duration

                times = current_time + transition_time + b._duration_offsets

                constraint_res = []
                for constraint in b._all_constraints:
                    constraint_result = constraint(self.observer, [b.target], times)
                    assert constraint_result >= 0
                    if constraint_result == 0:
                        vetoed = True
                    constraint_res.append(constraint_result)
                # take the product over all the constraints *and* times
                if not vetoed:
                    block_constraint_results.append(b.priority*np.sum(constraint_res))
                else:
                    block_constraint_results.append(0)

            # now identify the block that's the best
            bestblock_idx = np.argmax(block_constraint_results)

            if block_constraint_results[bestblock_idx] == 0.:
                # if even the best is unobservable, we need a gap
                new_blocks.append(TransitionBlock({'nothing_observable': self.gap_time}, current_time))
                current_time += self.gap_time
            else:
                # If there's a best one that's observable, first get its transition
                trans = block_transitions.pop(bestblock_idx)
                if trans is not None:
                    new_blocks.append(trans)
                    current_time += trans.duration

                # now assign the block itself times and add it to the schedule
                newb = blocks.pop(bestblock_idx)
                newb.start_time = current_time
                current_time += self.gap_time
                newb.end_time = current_time
                newb.constraints_value = block_constraint_results[bestblock_idx]

                new_blocks.append(newb)

        return new_blocks, True


class Transitioner(object):
    """
    A class that defines how to compute transition times from one block to 
    another.

    Parameters
    ----------
    slew_rate : `~astropy.units.Quantity` with angle/time units
        The slew rate of the telescope
    instrument_reconfig_times : dict of dicts or None
        If not None, gives a mapping from property names to another dictionary.
        The second dictionary maps 2-tuples of states to the time it takes to
        transition between those states (as an `~astropy.units.Quantity`).

    """
    u.quantity_input(slew_rate=u.deg/u.second)
    def __init__(self, slew_rate=None, instrument_reconfig_times=None):
        self.slew_rate = slew_rate
        self.instrument_reconfig_times = instrument_reconfig_times


    def __call__(self, oldblock, newblock, start_time, observer):
        """
        Determines the amount of time needed to transition from one observing
        block to another.  This uses the parameters defined in 
        ``self.instrument_reconfig_times``.

        Parameters
        ----------
        oldblock : `ObservingBlock` or None
            The initial configuration/target
        newblock : `ObservingBlock` or None
            The new configuration/target to transition to
        start_time : `~astropy.time.Time`
            The time the transition should start
        observer : `astroplan.Observer`
            The observer at the time 

        Returns
        -------
        transition : `TransitionBlock` or None
            A transition to get from `oldblock` to `newblock` or None if no
            transition is necessary
        """
        components = {}
        if self.slew_rate is not None:
            # use the constraints cache for now, but should move that machinery to
            # observer
            from .constraints import _get_altaz
            from astropy.time import Time

            if type(oldblock) == ObservingBlock:
                aaz = _get_altaz(Time([start_time]), observer, [oldblock.target, newblock.target])['altaz']
                # TODO: make this [0] unnecessary by fixing _get_altaz to behave well in scalar-time case
                sep = aaz[0].separation(aaz[1])[0]
                components['slew_time'] = sep / self.slew_rate
            elif type(oldblock) == TransitionBlock:
                # if the previous block was a TransitionBlock, then assume slew
                # hapens during that block
                components['slew_time'] = 0.
        if self.instrument_reconfig_times is not None:
            components.update(self.compute_instrument_transitions(oldblock, newblock))

        if components:
            return TransitionBlock(components, start_time)
        else:
            return None

        def compute_instrument_transitions(self, oldblock, newblock):
            components = {}
            for conf_name, old_conf in oldblock.configuration.items():
                if conf_name in newblock:
                    conf_times = self.instrument_reconfig_times.get(conf_name, None)
                    if conf_times is not None:
                        new_conf = newblock[conf_name]
                        ctime = conf_times.get((old_conf, new_conf), None)
                        if ctime is not None:
                            s = '{0}:{1} to {2}'.format(conf_name, old_conf, new_conf)
                            components[s] = ctime
            return components





