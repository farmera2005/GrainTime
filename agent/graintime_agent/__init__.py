"""GrainTime Agent: reads tickets from scale software whose database only
programs on the scale PC can open (AgVantagePC), and sends them to GrainTime.

Read-only by construction: it runs only SELECT statements it builds itself
from the site's mapping profile (graintime.collector.mapping.SiteReader), and
a connection wrapper refuses anything else. It never starts or keeps open the
scale software's database: it reads only while that database is already
running, with one short connection per poll.
"""

VERSION = "1.0.0"
