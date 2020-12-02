import copy
import datetime

import numpy as np
import pandas as pd


def smoothen_triplegs(triplegs, method='douglas-peucker', tolerance=1.0):
    """reduces number of points while retaining structure of tripleg
    Parameters
    ----------
    triplegs: shapely file
        triplegs to be reduced
    method: method used to smoothen
        only the douglas-peucker method is available so far
    tolerance: float
        a higher tolerance removes more points; the units of tolerance are the same as the projection of the input geometry
    """
    input_copy = copy.deepcopy(triplegs)
    origin_geom = input_copy.geom
    simplified_geom = origin_geom.simplify(tolerance, preserve_topology=False)
    input_copy.geom = simplified_geom

    return input_copy

def _temp_trip_stack_has_tripleg(temp_trip_stack):
    """
    Check if a trip has at least 1 tripleg
    Parameters
    ----------
        temp_trip_stack : list
                    list of dictionary like elements (either pandas series or
                    python dictionary). Contains all elements
                    that will be aggregated into a trip

    Returns
    -------
    Bool
    """

    has_tripleg = False
    for row in temp_trip_stack:
        if row['type'] == 'tripleg':
            has_tripleg = True
            break

    return has_tripleg


def _create_trip_from_stack(temp_trip_stack, origin_activity, destination_activity, trip_id_counter):
    """
    Aggregate information of trip elements in a structured dictionary

    Parameters
    ----------
    temp_trip_stack : list
                    list of dictionary like elements (either pandas series or python dictionary). Contains all elements
                    that will be aggregated into a trip
    origin_activity : dictionary like
                    Either dictionary or pandas series
    destination_activity : dictionary like
                    Either dictionary or pandas series
    trip_id_counter : int
            current trip id

    Returns
    -------
    dictionary

    """

    # this function return and empty dict if no tripleg is in the stack
    first_trip_element = temp_trip_stack[0]
    last_trip_element = temp_trip_stack[-1]

    # all data has to be from the same user
    assert origin_activity['user_id'] == last_trip_element['user_id']

    # double check if trip requirements are fulfilled
    assert origin_activity['activity'] == True
    assert destination_activity['activity'] == True
    assert first_trip_element['activity'] == False

    trip_dict_entry = {'id': trip_id_counter,
                       'user_id': origin_activity['user_id'],
                       'started_at': first_trip_element['started_at'],
                       'finished_at': last_trip_element['finished_at'],
                       'origin_staypoint_id': origin_activity['id'],
                       'destination_staypoint_id': destination_activity['id']}

    return trip_dict_entry


def _return_ids_to_df(temp_trip_stack, origin_activity, destination_activity, spts, tpls, trip_id_counter):
    """
    Write trip ids into the staypoint and tripleg GeoDataFrames.

    Parameters
    ----------
    temp_trip_stack : list
                    list of dictionary like elements (either pandas series or python dictionary). Contains all elements
                    that will be aggregated into a trip
    origin_activity : dictionary like
                    Either dictionary or pandas series
    destination_activity : dictionary like
                    Either dictionary or pandas series
    spts : GeoDataFrame
            Staypoints
    tpls :
            Triplegs
    trip_id_counter : int
            current trip id

    Returns
    -------
    None
        Function alters the staypoint and tripleg GeoDataFrames inplace
    """

    spts.loc[spts.index == origin_activity['id'], ['next_trip_id']] = trip_id_counter
    spts.loc[spts.index == destination_activity['id'], ['prev_trip_id']] = trip_id_counter

    for row in temp_trip_stack:
        if row['type'] == 'tripleg':
            tpls.loc[tpls.index == row['id'], ['trip_id']] = trip_id_counter
        elif row['type'] == 'staypoint':
            spts.loc[spts.index == row['id'], ['trip_id']] = trip_id_counter


def generate_trips(stps_input, tpls_input, gap_threshold=15, id_offset=0, print_progress=False):
    """ Generate trips based on staypoints and triplegs

    `generate_trips` aggregates the staypoints `stps_input` and `tpls_input` into `trips` which are returned
    in a new DataFrame. The function returns new versions of `stps_input` and `tpls_input` that are identically except
    for additional id's that allow the matching between staypoints, triplegs and trips.

    Parameters
    ----------
    stps_input : GeoDataFrame
                Staypoints that are used for the trip generation
    tpls_input : GeoDataFrame
                Triplegs that are used for the trip generation
    gap_threshold : float
                Maximum allowed temporal gap size in minutes. If tracking data is misisng for more than `gap_threshold`
                minutes, then a new trip begins after the gap.
    id_offset : int
                IDs for trips are incremented starting from this value.

    Returns
    -------
    (GeoDataFrame, GeoDataFrame, GeoDataFrame)
        the tuple contains (staypoints, triplegs, trips)

    Notes
    -----
    Trips are an aggregation level in transport planning that summarize all movement and all non-essential actions
    (e.g., waiting) between two relevant activities.
    The function returns altered versions of the input staypoints and triplegs. Staypoints receive the fields
    [`trip_id` `prev_trip_id` and `next_trip_id`], triplegs receive the field [`trip_id`].
    The following assumptions are implemented
        - All movement before the first and after the last activity is omitted
        - If we do not record a person for more than `gap_threshold` minutes, we assume that the person performed
         an activity in the recording gap and split the trip at the gap.
        - Trips that start/end in a recording gap can have an unknown origin/destination
        - There are no trips without a (recored) tripleg

    Examples
    ---------
    >>> staypoints, triplegs, trips = generate_trips(staypoints, triplegs)

    """
    assert 'activity' in stps_input.columns, "staypoints need the column 'activities' \
                                         to be able to generate trips"

    # we copy the input because we need to add a temporary column
    tpls = tpls_input.copy()
    spts = stps_input.copy()

    trip_id_counter = id_offset
    tpls['type'] = 'tripleg'
    spts['type'] = 'staypoint'
    spts['prev_trip_id'] = np.nan
    spts['next_trip_id'] = np.nan
    spts['trip_id'] = np.nan
    tpls['trip_id'] = np.nan

    trips_of_user_list = []
    dont_print_list = []

    # create table with relevant information from triplegs and staypoints.
    spts_tpls = spts[['started_at', 'finished_at', 'user_id', 'type', 'activity']].append(
        tpls[['started_at', 'finished_at', 'user_id', 'type']])

    # create ID field from index
    spts_tpls['id'] = spts_tpls.index

    # transform nan to bool
    spts_tpls['activity'] = spts_tpls['activity'] == True

    spts_tpls.sort_values(by=['user_id', 'started_at'], inplace=True)
    spts_tpls['started_at_next'] = spts_tpls['started_at'].shift(-1)
    spts_tpls['activity_next'] = spts_tpls['activity'].shift(-1)

    for user_id_this in spts_tpls['user_id'].unique():
        unknown_activity = {'user_id': user_id_this, 'activity': True, 'id': np.nan}

        spts_tpls_this = spts_tpls[spts_tpls['user_id'] == user_id_this]
        # assert (spts_tpls_this['started_at'].is_monotonic)  # this is expensive and should be replaced

        origin_activity = unknown_activity
        temp_trip_stack = []
        before_first_trip = True
        in_trip = False

        for _, row in spts_tpls_this.iterrows():
            if print_progress:
                if trip_id_counter % 100 == 0:
                    if not trip_id_counter in dont_print_list:
                        print("trip number: {}".format(trip_id_counter))
                        dont_print_list.append(trip_id_counter)

            # check if we can start a new trip
            # (we make sure that we start the trip with the most recent activity)
            if in_trip is False:
                # If there are several activities in a row, we skip until the last one
                if row['activity'] and row['activity_next']:
                    continue

                # if this is the last activity before the trip starts, reset the origin
                elif row['activity']:
                    origin_activity = row
                    in_trip = True
                    continue

                # if for non-activities we simply start the trip
                else:
                    in_trip = True

            if in_trip is True:
                # during trip generation/recording

                # check if trip ends regularly
                if row['activity'] is True:

                    # if there are no triplegs in the trip, set the current activity as origin and start over
                    if not _temp_trip_stack_has_tripleg(temp_trip_stack):
                        origin_activity = row
                        temp_trip_stack = list()
                        in_trip = True

                    else:
                        # record trip
                        destination_activity = row
                        trips_of_user_list.append(_create_trip_from_stack(temp_trip_stack, origin_activity,
                                                                          destination_activity, trip_id_counter))
                        _return_ids_to_df(temp_trip_stack, origin_activity, destination_activity,
                                          spts, tpls, trip_id_counter)
                        trip_id_counter += 1

                        # set values for next trip
                        if row['started_at_next'] - row['finished_at'] > datetime.timedelta(minutes=gap_threshold):
                            # if there is a gap after this trip the origin of the next trip is unknown
                            origin_activity = unknown_activity
                            destination_activity = None
                            temp_trip_stack = list()
                            in_trip = False

                        else:
                            # if there is no gap after this trip the origin of the next trip is the destination of the
                            # current trip
                            origin_activity = destination_activity
                            destination_activity = None
                            temp_trip_stack = list()
                            in_trip = False

                # check if gap during the trip
                elif row['started_at_next'] - row['finished_at'] > datetime.timedelta(minutes=gap_threshold):
                    # in case of a gap, the destination of the current trip and the origin of the next trip
                    # are unknown.

                    # add current item to trip
                    temp_trip_stack.append(row)

                    # if the trip has no recored triplegs, we do not generate the current trip.
                    if not _temp_trip_stack_has_tripleg(temp_trip_stack):
                        origin_activity = unknown_activity
                        in_trip = True
                        temp_trip_stack = list()

                    else:
                        # add tripleg to trip, generate trip, start new trip with unknown origin
                        destination_activity = unknown_activity

                        trips_of_user_list.append(_create_trip_from_stack(temp_trip_stack, origin_activity,
                                                                          destination_activity,
                                                                          trip_id_counter))
                        _return_ids_to_df(temp_trip_stack, origin_activity, destination_activity,
                                          spts, tpls, trip_id_counter)

                        trip_id_counter += 1
                        origin_activity = unknown_activity
                        destination_activity = None
                        temp_trip_stack = list()
                        in_trip = True

                else:
                    temp_trip_stack.append(row)

        # if user ends generate last trip with unknown destination
        if (len(temp_trip_stack) > 0) and (_temp_trip_stack_has_tripleg(temp_trip_stack)):
            destination_activity = unknown_activity
            trips_of_user_list.append(_create_trip_from_stack(temp_trip_stack, origin_activity,
                                                              destination_activity,
                                                              trip_id_counter))
            _return_ids_to_df(temp_trip_stack, origin_activity, destination_activity,
                              spts, tpls, trip_id_counter)
            trip_id_counter += 1

    trips = pd.DataFrame(trips_of_user_list)
    tpls.drop(['type'], axis=1, inplace=True)
    spts.drop(['type'], axis=1, inplace=True)
    trips = trips.set_index('id')
    return spts, tpls, trips
