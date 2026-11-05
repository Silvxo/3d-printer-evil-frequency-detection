
The objective of the whole system is to detect ghosting in 3d printers. As hardware, a esp32 was paired with an ADXL345 accelerometer, whose responsible for gathering the data. Data will be sent through MQTT and must be read by this part of the system, which will be subscribed to the topic. 

Your job is to build the application that will live in this computer, which must subscribe to the MQTT Topic, listen, and store each string that was sent by the microcontroller. For now, it does not require a dashboard, some messages printed in the terminal are enough for the current state of the project.

The MQTT Broker settings are:
Host: 	broker.hivemq.com
TCP Port: 	1883
Websocket Port: 	8000
TLS TCP Port: 	8883
TLS Websocket Port: 	8884

topic to subscribe: 3d/printer/49801a48-1dd7-4301-901b-e0f87f44521a

The message format will be:
x;y;z;samplingRate





Now, the systems needs to:
- Save separate files for separated sections. The section name will be given by a UI later -- for now it just need to save in sequential numbers. Each time the application starts, one number will be added. Make it easier to, later, a UI insert a value there.
- The database must be a CSV or table like instead of Json. Thinking on implementing a fourrier transformation later, which way is the best one? Is implementing A SQLite viable?
- Build a simple HTML with two tabs:
    - Data capture: Has one field to receive the name of the data recording; One button to start, one button to pause and one button to pause.
    - Data analysis: Has two spaces for graphs, one to show all the spectre of frequencies and another with Fourrier Transform to display the ghost frequencies.
