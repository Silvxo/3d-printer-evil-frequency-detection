
The objective of the whole system is to detect ghosting in 3d printers. As hardware, a esp32 was paired with an ADXL345 accelerometer, whose responsible for gathering the data. Data will be sent through MQTT and must be read by this part of the system, which will be subscribed to the topic. 

Your job is to build the application that will live in this computer, which must subscribe to the MQTT Topic, listen, and store each string that was sent by the microcontroller. For now, it does not require a dashboard, some messages printed in the terminal are enough for the current state of the project.

The MQTT Broker settings are:
Host: 	broker.hivemq.com
TCP Port: 	1883
Websocket Port: 	8000
TLS TCP Port: 	8883
TLS Websocket Port: 	8884

topic to subscribe: 3d/printer/49801a48-1dd7-4301-901b-e0f87f44521a