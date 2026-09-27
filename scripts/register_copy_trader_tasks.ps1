# Re-registers the Politician Copy Trader and Senate Disclosures tasks
# (re-enabled 2026-09-26 at Nathaniel's request; both were deleted 2026-06-11).
#
# PoliticianCopyTrader : hourly Mon-Fri 9:30 AM - 6:00 PM
# SenateDisclosures    : every 2 hours Mon-Fri 9:30 AM - 6:00 PM

$py = 'C:\Users\Nathaniel\AppData\Local\Programs\Python\Python310\pythonw.exe'
$root = 'C:\Users\Nathaniel\Documents\Trading'

function Register-Job($name, $script, $workdir, $interval) {
$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>TAHOE\Nathaniel</Author>
    <URI>\Alpaca\$name</URI>
  </RegistrationInfo>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-21-897672513-2981516108-3790960897-1001</UserId>
      <LogonType>InteractiveToken</LogonType>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <Enabled>true</Enabled>
    <StartWhenAvailable>true</StartWhenAvailable>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
  </Settings>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-09-28T09:30:00</StartBoundary>
      <Repetition>
        <Interval>$interval</Interval>
        <Duration>PT8H30M</Duration>
      </Repetition>
      <ScheduleByWeek>
        <WeeksInterval>1</WeeksInterval>
        <DaysOfWeek>
          <Monday /><Tuesday /><Wednesday /><Thursday /><Friday />
        </DaysOfWeek>
      </ScheduleByWeek>
    </CalendarTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>$py</Command>
      <Arguments>$script</Arguments>
      <WorkingDirectory>$workdir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
    $xmlPath = "$env:TEMP\$name.xml"
    $xml | Out-File -FilePath $xmlPath -Encoding Unicode
    schtasks /create /tn "\Alpaca\$name" /xml $xmlPath /f
}

Register-Job 'PoliticianCopyTrader' "$root\politician-copy-trader\main.py" "$root\politician-copy-trader" 'PT1H'
Register-Job 'SenateDisclosures'    "$root\signals\senate_disclosures.py" "$root\signals" 'PT2H'
