# -*- coding: utf-8 -*-
"""
Make a VoD file look like infinite live DASH content. The timing is synchronized with wall clock.

The rewrites which are done are

MPD:
    @MPD
      remove mediaPresentationDuration
      set type dynamic
      set publishTime
      set timeShiftBufferDepth
      set availabilityStartTime
      set minimumUpdatePeriod
      set maxSegmentDuration
      set/add availabilityEndTIme
    @SegmentTemplate
      set startNumber

initialization segments:
   No change

Media segments
   Mapped from live number to VoD number
   tfdt and sidx updated to match live time (if KEEP_SIDX = true)
   sequenceNumber updated to be continuous (and identical to the sequenceNumber asked for)

The numbering and timing is based on the epoch time, and is generally

[time_in_epoch clipped to multiple of duration]/duration

Thus segNr corresponds to the interval [segNr*duration , (segNr+1)*duration]

For infinite content, the default is startNumber = 0, availabilityStartTime = 1970-01-01T00:00:00
"""

# The copyright in this software is being made available under the BSD License,
# included below. This software may be subject to other third party and contributor
# rights, including patent rights, and no such rights are granted under this license.
#
# Copyright (c) 2015, Dash Industry Forum.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#  * Redistributions of source code must retain the above copyright notice, this
#  list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above copyright notice,
#  this list of conditions and the following disclaimer in the documentation and/or
#  other materials provided with the distribution.
#  * Neither the name of Dash Industry Forum nor the names of its
#  contributors may be used to endorse or promote products derived from this software
#  without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS AS IS AND ANY
#  EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#  WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED.
#  IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
#  INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT
#  NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
#  PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
#  WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.

from os.path import splitext, join
from math import ceil
from re import findall
from .initsegmentfilter import InitLiveFilter
from .mediasegmentfilter import MediaSegmentFilter
from . import segmentmuxer
from . import mpdprocessor
from .timeformatconversions import make_timestamp, seconds_to_iso_duration
from .configprocessor import ConfigProcessor
from xml.etree import ElementTree as ET
import time

SECS_IN_DAY = 24 * 3600
DEFAULT_MINIMUM_UPDATE_PERIOD = "P100Y"
DEFAULT_PUBLISH_ADVANCE_IN_S = 7200
EXTRA_TIME_AFTER_END_IN_S = 60

UTC_HEAD_PATH = "dash/time.txt"

PUBLISH_TIME = False


def handle_request(host_name, url_parts, args, vod_conf_dir, content_dir, now=None, req=None, is_https=0):
    "Handle Apache request."
    dash_provider = DashProvider(host_name, url_parts, args, vod_conf_dir, content_dir, now, req, is_https)
    return dash_provider.handle_request()


class DashProxyError(Exception):
    "Error in DashProxy."


class DashSegmentNotAvailableError(DashProxyError):
    "Segment not available."


def generate_period_data(base_data, mpd_data, now):
    """Generate an array of period data depending on current time (now) and tsbd. 0 gives one period with start=1000h.

    mpd_data is changed (minimumUpdatePeriod)."""
    period_data = []
    start = 0
    i = 0
    for data in base_data:
        start = data['start']
        timescale = data['timescale']
        seg_dur = data['segDuration']
        prd_duration = data['duration']
        start_number = data['startNumber'] + start / seg_dur
        data = {'id': "p%d" % start, 'periodDuration': 'PT%dS' % prd_duration, 'start': 'PT%dS' % start, 'startNumber': str(start_number),
            'duration': seg_dur, 'presentationTimeOffset': "%d" % (start*timescale),
            'start_s' : start}
        period_data.append(data)
        i += 1
    return period_data

def insert_asset_identifier(response, start_pos_period):
    ad_pos = response.find(">", start_pos_period) + 1
    response = response[:ad_pos] + "\n<AssetIdentifier schemeIdUri=\"urn:org:dashif:asset-id:2013\" value=\"md:cid:" \
                                   "EIDR:10.5240%2f0EFB-02CD-126E-8092-1E49-W\"></AssetIdentifier>" + response[ad_pos:]
    return response


class DashProvider(object):
    "Provide DASH manifest and segments."

    # pylint: disable=too-many-instance-attributes,too-many-arguments

    def __init__(self, host_name, url_parts, url_args, vod_conf_dir, content_dir, now=None, req=None, is_https=0):
        protocol = is_https and "https" or "http"
        self.base_url = "%s://%s/%s/" % (protocol, host_name, url_parts[0])  # The start. Adding other parts later.
        self.utc_head_url = "%s://%s/%s" % (protocol, host_name, UTC_HEAD_PATH)
        self.url_parts = url_parts[1:]
        self.url_args = url_args
        self.vod_conf_dir = vod_conf_dir
        self.content_dir = content_dir
        self.now_float = now  # float
        self.now = int(now)
        self.req = req
        self.new_tfdt_value = None

    def handle_request(self):
        "Handle the HTTP request."
        return self.parse_url()

    def error_response(self, msg):
        "Return a mod_python error response."
        if self.req:
            self.req.log_error("dash_proxy: [%s] %s" % ("/".join(self.url_parts[-3:]), msg))
        return {'ok': False, 'pl': msg + "\n"}

    # pylint:disable = too-many-locals, too-many-branches
    def parse_url(self):
        "Parse the absolute URL that is received in mod_python."
        cfg_processor = ConfigProcessor(self.vod_conf_dir, self.base_url)
        cfg_processor.process_url(self.url_parts, self.now)
        cfg = cfg_processor.getconfig()
        self.sort_by_period(cfg, self.now)
        if cfg.ext == ".mpd":
            mpd_filenames = []
            for filename in cfg.filenames:
                mpd_filenames.append("%s/%s/%s" % (self.content_dir, cfg.content_name, filename))
            mpd_input_data = cfg_processor.get_mpd_data() # Becomes an array
            response = self.generate_dynamic_mpd(cfg, mpd_filenames, mpd_input_data, self.now)
        elif cfg.ext == ".mp4":
            if self.now < cfg.availability_start_time_in_s - cfg.init_seg_avail_offset:
                diff = (cfg.availability_start_time_in_s - cfg.init_seg_avail_offset) - self.now_float
                response = self.error_response("Request for %s was %.1fs too early" % (cfg.filenames[0], diff))
            else:
                response = self.process_init_segment(cfg)
        elif cfg.ext == ".m4s":
            if cfg.availability_time_offset_in_s == -1:
                first_segment_ast = cfg.availability_start_time_in_s
            else:
                first_segment_ast = cfg.availability_start_time_in_s + cfg.vod_infos[0].seg_duration - \
                                    cfg.availability_time_offset_in_s
            if self.now_float < first_segment_ast:
                diff = first_segment_ast - self.now_float
                response = self.error_response("Request %s before first seg AST. %.1fs too early." %
                                               (cfg.filenames[0], diff))
            elif cfg.availability_end_time is not None and \
                            self.now > cfg.availability_end_time + EXTRA_TIME_AFTER_END_IN_S:
                diff = self.now_float - (cfg.availability_end_time + EXTRA_TIME_AFTER_END_IN_S)
                response = self.error_response("Request for %s after AET. %.1fs too late" % (cfg.filenames[0], diff))
            else:
                response = self.process_media_segment(cfg, self.now_float)
                if len(cfg.multi_url) == 1:  # There is one specific baseURL with losses specified
                    a_var, b_var = cfg.multi_url[0].split("_")
                    dur1 = int(a_var[1:])
                    dur2 = int(b_var[1:])
                    total_dur = dur1 + dur2
                    num_loop = int(ceil(60.0 / (float(total_dur))))
                    now_mod_60 = self.now % 60
                    if a_var[0] == 'u' and b_var[0] == 'd':  # parse server up or down information
                        for i in range(num_loop):
                            if i * total_dur + dur1 < now_mod_60 <= (i + 1) * total_dur:
                                response = self.error_response("BaseURL server down at %d" % (self.now))
                                break
                    elif a_var[0] == 'd' and b_var[0] == 'u':
                        for i in range(num_loop):
                            if i * (total_dur) < now_mod_60 <= i * (total_dur) + dur1:
                                response = self.error_response("BaseURL server down at %d" % (self.now))
                                break
                time.sleep(.500)
        else:
            response = "Unknown file extension: %s" % cfg.ext
        return response

    def sort_by_period(self, cfg, now):
        total = 0
        prog = 0
        for vod_info in cfg.vod_infos:
            duration = vod_info.wrap_seconds
            total += duration
        diff = now - cfg.availability_start_time_in_s
        loops, time = divmod(diff, total)
        for idx, vod_info in enumerate(cfg.vod_infos):
            duration = vod_info.wrap_seconds
            prog += duration
            if(time > prog + vod_info.seg_duration):
                cfg.vod_infos.append(cfg.vod_infos[0])
                cfg.filenames.append(cfg.filenames[0])
                cfg.start_from_ast += cfg.vod_infos[0].wrap_seconds
                cfg.vod_infos.pop(0)
                cfg.filenames.pop(0)
                break
        if now - cfg.availability_start_time_in_s > cfg.vod_infos[0].wrap_seconds:
            first_vod_infos = cfg.vod_infos[-1]
            cfg.vod_infos = [first_vod_infos] + cfg.vod_infos
            first_filename = cfg.filenames[-1]
            cfg.filenames = [first_filename] + cfg.filenames
            cfg.start_from_ast -= cfg.vod_infos[0].wrap_seconds
        cfg.start_from_ast += (loops*total)

    # pylint: disable=no-self-use
    def generate_dynamic_mpd(self, cfg, mpd_filenames, in_data, now):
        "Generate the dynamic MPD."
        mpd_data = in_data.copy()
        if cfg.minimum_update_period_in_s is not None:
            mpd_data['minimumUpdatePeriod'] = seconds_to_iso_duration(cfg.minimum_update_period_in_s)
        else:
            mpd_data['minimumUpdatePeriod'] = DEFAULT_MINIMUM_UPDATE_PERIOD
        if cfg.media_presentation_duration is not None:
            mpd_data['mediaPresentationDuration'] = seconds_to_iso_duration(cfg.media_presentation_duration)
        mpd_data['timeShiftBufferDepth'] = seconds_to_iso_duration(cfg.timeshift_buffer_depth_in_s)
        mpd_data['timeShiftBufferDepthInS'] = cfg.timeshift_buffer_depth_in_s
        mpd_data['publishTime'] = '%s' % make_timestamp(in_data['publishTime'])
        mpd_data['availabilityStartTime'] = '%s' % make_timestamp(in_data['availability_start_time_in_s'])
        mpd_data['presentationTimeOffset'] = 0
        mpd_data['availabilityTimeOffset'] = '%f' % in_data['availability_time_offset_in_s']
        if in_data.has_key('availabilityEndTime'):
            mpd_data['availabilityEndTime'] = make_timestamp(in_data['availabilityEndTime'])
        mpd_proc_cfg = {'scte35Present': (cfg.scte35_per_minute > 0),
                        'continuous': in_data['continuous'],
                        'utc_timing_methods': cfg.utc_timing_methods,
                        'utc_head_url': self.utc_head_url,
                        'now': now}

        start = cfg.start_from_ast
        base_data = []
        rebuilt_filenames = []
        for i in range(len(cfg.vod_infos)):
            mediaData = cfg.vod_infos[i].media_data
            total_duration = mediaData['video']['total_duration']
            timescale = mediaData['video']['timescale']
            timeinseconds = total_duration / timescale
            base_data.append({'timescale': timescale, 'start': start, 'duration': timeinseconds, 'segDuration': cfg.vod_infos[i].seg_duration, 'startNumber': cfg.start_nr})
            start += timeinseconds
            rebuilt_filenames.append(mpd_filenames[i])
        mpmod = mpdprocessor.MpdProcessor(rebuilt_filenames, mpd_proc_cfg, cfg)
        period_data = generate_period_data(base_data, mpd_data, now)
        mpmod.process(mpd_data, period_data)
        return mpmod.get_full_xml()

    def process_init_segment(self, cfg):
        "Read non-multiplexed or create muxed init segments."

        nr_reps = len(cfg.reps)
        if nr_reps == 1:  # Not muxed
            init_file = "%s/%s/%s/%s" % (self.content_dir, cfg.content_name, cfg.rel_path, cfg.filenames[0])
            ilf = InitLiveFilter(init_file)
            data = ilf.filter()
        elif nr_reps == 2:  # Something that can be muxed
            com_path = "/".join(cfg.rel_path.split("/")[:-1])
            init1 = "%s/%s/%s/%s/%s" % (self.content_dir, cfg.content_name, com_path, cfg.reps[0]['id'], cfg.filenames[0])
            init2 = "%s/%s/%s/%s/%s" % (self.content_dir, cfg.content_name, com_path, cfg.reps[1]['id'], cfg.filenames[0])
            muxed_inits = segmentmuxer.MultiplexInits(init1, init2)
            data = muxed_inits.construct_muxed()
        else:
            data = self.error_response("Bad nr of representations: %d" % nr_reps)
        return data

    def process_media_segment(self, cfg, now_float):
        
        """Process media segment. Return error response if timing is not OK.

        Assumes that segment_ast = (seg_nr+1-startNumber)*seg_dur."""

        # pylint: disable=too-many-locals

        def get_timescale(cfg):
            "Get timescale for the current representation."
            timescale = None
            curr_rep_id = cfg.repId
            for rep in cfg.reps:
                if rep['id'] == curr_rep_id:
                    timescale = rep['timescale']
                    break
            return timescale
        
        seg_dur = cfg.vod_infos[0].seg_duration
        seg_name = cfg.filenames[0]
        seg_base, seg_ext = splitext(seg_name)
        timescale = get_timescale(cfg)
        if seg_base[0] == 't':
            # TODO. Make a more accurate test here that the timestamp is a correct one
            seg_nr = int(round(float(seg_base[1:]) / seg_dur / timescale))
        else:
            seg_nr = int(seg_base)
        seg_start_nr = cfg.start_nr == -1 and 1 or cfg.start_nr
        if seg_nr < seg_start_nr:
            return self.error_response("Request for segment %d before first %d" % (seg_nr, seg_start_nr))
        if len(cfg.last_segment_numbers) > 0:
            very_last_segment = cfg.last_segment_numbers[-1]
            if seg_nr > very_last_segment:
                return self.error_response("Request for segment %d beyond last (%d)" % (seg_nr, very_last_segment))
        lmsg = seg_nr in cfg.last_segment_numbers
        # print cfg.last_segment_numbers
        seg_time = (seg_nr - seg_start_nr) * seg_dur + cfg.availability_start_time_in_s
        seg_ast = seg_time + seg_dur
        # if cfg.availability_time_offset_in_s != -1:
        #     if now_float < seg_ast - cfg.availability_time_offset_in_s:
        #         return self.error_response("Request for %s was %.1fs too early" % (seg_name, seg_ast - now_float))
        #     if now_float > seg_ast + seg_dur + cfg.timeshift_buffer_depth_in_s:
        #         diff = now_float - (seg_ast + seg_dur + cfg.timeshift_buffer_depth_in_s)
        #         return self.error_response("Request for %s was %.1fs too late" % (seg_name, diff))
        time_since_ast = seg_time - cfg.availability_start_time_in_s
        multiple_durations = cfg.initial_durations
        loop_duration = cfg.loop_duration
        diff = seg_time - cfg.availability_start_time_in_s
        loops, time = divmod(diff, loop_duration)
        index = 0
        last = 0
        i = 0

        if time >= multiple_durations[0]:
            last = multiple_durations[0]

        seg_nr_in_loop = int((time - last) / seg_dur)
        offset_at_loop_start = (loops * loop_duration) + last 
        vod_nr = seg_nr_in_loop + cfg.vod_infos[0].first_segment_in_loop
        # assert 0 <= vod_nr - cfg.vod_first_segment_in_loop[0] < cfg.vod_nr_segments_in_loop[0]
        rel_path = cfg.rel_path # XXX VOIR LOGIQUE REP 1 // MULTIPLE CONTENUS
        nr_reps = len(cfg.reps)
        if nr_reps == 1:  # Not muxed
            seg_content = self.filter_media_segment(cfg, cfg.reps[0], rel_path, vod_nr, seg_nr, seg_ext,
                                                    offset_at_loop_start, lmsg)
        else:
            rel_path_parts = rel_path.split("/")
            common_path_parts = rel_path_parts[:-1]
            rel_path1 = "/".join(common_path_parts + [cfg.reps[0]['id']])
            rel_path2 = "/".join(common_path_parts + [cfg.reps[1]['id']])
            seg1 = self.filter_media_segment(cfg, cfg.reps[0], rel_path1, vod_nr, seg_nr, seg_ext,
                                             offset_at_loop_start, lmsg)
            seg2 = self.filter_media_segment(cfg, cfg.reps[1], rel_path2, vod_nr, seg_nr, seg_ext,
                                             offset_at_loop_start, lmsg)
            muxed = segmentmuxer.MultiplexMediaSegments(data1=seg1, data2=seg2)
            seg_content = muxed.mux_on_sample_level()
        return seg_content

    # pylint: disable=too-many-arguments
    def filter_media_segment(self, cfg, rep, rel_path, vod_nr, seg_nr, seg_ext, offset_at_loop_start, lmsg):
        "Filter an actual media segment by using time-scale from init segment."
        media_seg_file = join(self.content_dir, cfg.content_name, rel_path, "%d%s" % (vod_nr, seg_ext))
        timescale = rep['timescale']
        scte35_per_minute = (rep['content_type'] == 'video') and cfg.scte35_per_minute or 0
        is_ttml = rep['content_type'] == 'subtitles'
        seg_filter = MediaSegmentFilter(media_seg_file, seg_nr, cfg.vod_infos[0].seg_duration, offset_at_loop_start, lmsg, timescale,
                                        scte35_per_minute, rel_path, is_ttml)
        seg_content = seg_filter.filter()
        self.new_tfdt_value = seg_filter.get_tfdt_value()
        return seg_content
