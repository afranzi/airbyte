#
# MIT License
#
# Copyright (c) 2020 Airbyte
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#

from abc import ABC, abstractmethod
from typing import Any, Iterable, Mapping, MutableMapping, Optional
from urllib.parse import parse_qsl, urlparse

import pendulum
import requests
from airbyte_cdk.models import SyncMode
from airbyte_cdk.sources.streams.http import HttpStream


class TwilioStream(HttpStream, ABC):
    url_base = "https://api.twilio.com/2010-04-01/"
    primary_key = "sid"
    page_size = 100

    @property
    def data_field(self):
        return self.name

    def path(self, **kwargs):
        return f"{self.name.title()}.json"

    def next_page_token(self, response: requests.Response) -> Optional[Mapping[str, Any]]:
        stream_data = response.json()
        next_page_uri = stream_data.get("next_page_uri")
        if next_page_uri:
            next_url = urlparse(next_page_uri)
            next_page_params = dict(parse_qsl(next_url.query))
            return next_page_params

    def parse_response(self, response: requests.Response, stream_state: Mapping[str, Any], **kwargs) -> Iterable[Mapping]:
        """
        :return an iterable containing each record in the response
        """
        records = response.json().get(self.data_field, [])
        yield from records

    def request_params(
            self,
            stream_state: Mapping[str, Any],
            next_page_token: Mapping[str, Any] = None,
            **kwargs
    ) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state=stream_state, next_page_token=next_page_token, **kwargs)
        params["PageSize"] = self.page_size
        if next_page_token:
            params.update(**next_page_token)
        return params


class IncrementalTwilioStream(TwilioStream, ABC):
    cursor_field = "date_updated"
    time_filter_template = "%Y-%m-%dT%H:%M:%SZ"

    def __init__(self, start_date: str = None, **kwargs):
        super().__init__(**kwargs)
        self._start_date = start_date

    @property
    @abstractmethod
    def incremental_filter_field(self) -> str:
        """
        return: date filter query parameter name
        """

    def get_updated_state(self, current_stream_state: MutableMapping[str, Any], latest_record: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        Return the latest state by comparing the cursor value in the latest record with the stream's most recent state object
        and returning an updated state object.
        """
        latest_benchmark = pendulum.parse(latest_record[self.cursor_field], strict=False).strftime(self.time_filter_template)
        if current_stream_state.get(self.cursor_field):
            return {self.cursor_field: max(latest_benchmark, current_stream_state[self.cursor_field])}
        return {self.cursor_field: latest_benchmark}

    def request_params(self, stream_state: Mapping[str, Any], **kwargs) -> MutableMapping[str, Any]:
        params = super().request_params(stream_state=stream_state, **kwargs)
        start_date = stream_state.get(self.cursor_field) or self._start_date
        if start_date:
            params.update({self.incremental_filter_field: pendulum.parse(start_date).strftime(self.time_filter_template)})
        return params


class TwilioNestedStream(TwilioStream):
    url_base = "https://api.twilio.com"
    media_exist_validation = {}

    def path(self, stream_slice: Mapping[str, Any], **kwargs):
        return stream_slice["subresource_uri"]

    @property
    def subresource_uri_key(self):
        return self.data_field

    @property
    @abstractmethod
    def parent_stream(self) -> TwilioStream:
        """
        :return: parent stream class
        """

    def stream_slices(self, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        stream_instance = self.parent_stream(authenticator=self.authenticator)
        stream_slices = stream_instance.stream_slices(sync_mode=SyncMode.full_refresh, cursor_field=stream_instance.cursor_field)
        for stream_slice in stream_slices:
            for item in stream_instance.read_records(
                sync_mode=SyncMode.full_refresh, stream_slice=stream_slice, cursor_field=stream_instance.cursor_field
            ):
                if item.get("subresource_uris", {}).get(self.subresource_uri_key):
                    validated = True
                    for key, value in self.media_exist_validation.items():
                        validated = item.get(key) and item.get(key) != value
                        if not validated:
                            break
                    if validated:
                        yield {"subresource_uri": item["subresource_uris"][self.subresource_uri_key]}


class Accounts(TwilioStream):
    """https://www.twilio.com/docs/usage/api/account#read-multiple-account-resources"""


class Addresses(TwilioNestedStream):
    """https://www.twilio.com/docs/usage/api/address#read-multiple-address-resources"""

    parent_stream = Accounts


class DependentPhoneNumbers(TwilioNestedStream):
    """https://www.twilio.com/docs/usage/api/address?code-sample=code-list-dependent-pns-subresources&code-language=curl&code-sdk-version=json#instance-subresources"""

    parent_stream = Addresses
    url_base = "https://api.twilio.com/2010-04-01/"

    def path(self, stream_slice: Mapping[str, Any], **kwargs):
        return f"Accounts/{stream_slice['account_sid']}/Addresses/{stream_slice['sid']}/DependentPhoneNumbers.json"

    def stream_slices(self, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        stream_instance = self.parent_stream(authenticator=self.authenticator)
        stream_slices = stream_instance.stream_slices(sync_mode=SyncMode.full_refresh, cursor_field=stream_instance.cursor_field)
        for stream_slice in stream_slices:
            for item in stream_instance.read_records(
                sync_mode=SyncMode.full_refresh, stream_slice=stream_slice, cursor_field=stream_instance.cursor_field
            ):
                yield {"sid": item["sid"], "account_sid": item["account_sid"]}


class Applications(TwilioNestedStream):
    """https://www.twilio.com/docs/usage/api/applications#read-multiple-application-resources"""

    parent_stream = Accounts


class AvailablePhoneNumberCountries(TwilioNestedStream):
    """https://www.twilio.com/docs/phone-numbers/api/availablephonenumber-resource#read-a-list-of-countries"""

    parent_stream = Accounts
    data_field = "countries"
    subresource_uri_key = "available_phone_numbers"


class AvailablePhoneNumbersLocal(TwilioNestedStream):
    """https://www.twilio.com/docs/phone-numbers/api/availablephonenumberlocal-resource#read-multiple-availablephonenumberlocal-resources"""

    parent_stream = AvailablePhoneNumberCountries
    data_field = "available_phone_numbers"
    subresource_uri_key = "local"


class AvailablePhoneNumbersMobile(TwilioNestedStream):
    """https://www.twilio.com/docs/phone-numbers/api/availablephonenumber-mobile-resource#read-multiple-availablephonenumbermobile-resources"""

    parent_stream = AvailablePhoneNumberCountries
    data_field = "available_phone_numbers"
    subresource_uri_key = "mobile"


class AvailablePhoneNumbersTollFree(TwilioNestedStream):
    """https://www.twilio.com/docs/phone-numbers/api/availablephonenumber-tollfree-resource#read-multiple-availablephonenumbertollfree-resources"""

    parent_stream = AvailablePhoneNumberCountries
    data_field = "available_phone_numbers"
    subresource_uri_key = "toll_free"


class IncomingPhoneNumbers(TwilioNestedStream):
    """https://www.twilio.com/docs/phone-numbers/api/incomingphonenumber-resource#read-multiple-incomingphonenumber-resources"""

    parent_stream = Accounts


class Keys(TwilioNestedStream):
    """https://www.twilio.com/docs/usage/api/keys#read-a-key-resource"""

    parent_stream = Accounts


class Calls(TwilioNestedStream, IncrementalTwilioStream):
    """https://www.twilio.com/docs/sms/api/message-resource#read-multiple-message-resources"""

    parent_stream = Accounts
    incremental_filter_field = "EndTime>"
    cursor_field = "end_time"


class Conferences(TwilioNestedStream, IncrementalTwilioStream):
    """https://www.twilio.com/docs/voice/api/conference-resource#read-multiple-conference-resources"""

    parent_stream = Accounts
    incremental_filter_field = "DateUpdated>="


class ConferenceParticipants(TwilioNestedStream):
    """https://www.twilio.com/docs/voice/api/conference-participant-resource#read-multiple-participant-resources"""

    parent_stream = Conferences
    data_field = "participants"


class OutgoingCallerIds(TwilioNestedStream):
    """https://www.twilio.com/docs/voice/api/outgoing-caller-ids#outgoingcallerids-list-resource"""

    parent_stream = Accounts


class Recordings(TwilioNestedStream, IncrementalTwilioStream):
    """https://www.twilio.com/docs/voice/api/recording#read-multiple-recording-resources"""

    parent_stream = Accounts
    incremental_filter_field = "DateCreated>"
    cursor_field = "date_created"


class Transcriptions(TwilioNestedStream):
    """https://www.twilio.com/docs/voice/api/recording-transcription?code-sample=code-read-list-all-transcriptions&code-language=curl&code-sdk-version=json#read-multiple-transcription-resources"""

    parent_stream = Accounts


class Queues(TwilioNestedStream):
    """https://www.twilio.com/docs/voice/api/queue-resource#read-multiple-queue-resources"""

    parent_stream = Accounts


class Messages(TwilioNestedStream, IncrementalTwilioStream):
    """https://www.twilio.com/docs/sms/api/message-resource#read-multiple-message-resources"""

    parent_stream = Accounts
    incremental_filter_field = "DateSent>"
    cursor_field = "date_sent"


class MessageMedia(TwilioNestedStream, IncrementalTwilioStream):
    """https://www.twilio.com/docs/sms/api/media-resource#read-multiple-media-resources"""

    parent_stream = Messages
    data_field = "media_list"
    subresource_uri_key = "media"
    media_exist_validation = {"num_media": "0"}
    incremental_filter_field = "StartTime>="


class UsageNestedStream(TwilioNestedStream):
    url_base = "https://api.twilio.com/2010-04-01/"
    time_filter_template = "%Y-%m-%d"

    @property
    @abstractmethod
    def path_name(self) -> str:
        """
        return: name of the end of the usage paths
        """

    def path(self, stream_slice: Mapping[str, Any], **kwargs):
        return f"Accounts/{stream_slice['account_sid']}/Usage/{self.path_name}.json"

    def stream_slices(self, **kwargs) -> Iterable[Optional[Mapping[str, any]]]:
        stream_instance = self.parent_stream(authenticator=self.authenticator)
        stream_slices = stream_instance.stream_slices(sync_mode=SyncMode.full_refresh, cursor_field=stream_instance.cursor_field)
        for stream_slice in stream_slices:
            for item in stream_instance.read_records(
                    sync_mode=SyncMode.full_refresh, stream_slice=stream_slice, cursor_field=stream_instance.cursor_field
            ):
                yield {"account_sid": item["sid"]}


class UsageRecords(UsageNestedStream, IncrementalTwilioStream):
    """https://www.twilio.com/docs/usage/api/usage-record#read-multiple-usagerecord-resources"""

    parent_stream = Accounts
    incremental_filter_field = "EndDate"
    cursor_field = "end_date"
    path_name = "Records"


class UsageTriggers(UsageNestedStream):
    """https://www.twilio.com/docs/usage/api/usage-trigger#read-multiple-usagetrigger-resources"""

    parent_stream = Accounts
    subresource_uri_key = "triggers"
    path_name = "Triggers"


class Alerts(IncrementalTwilioStream):
    """https://www.twilio.com/docs/usage/monitor-alert#read-multiple-alert-resources"""

    url_base = "https://monitor.twilio.com/v1/"
    incremental_filter_field = "StartDate"

    def path(self, **kwargs):
        return self.name.title()