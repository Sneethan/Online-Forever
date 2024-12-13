from typing import Literal

class Config:
    # Bot status: "online", "dnd", or "idle"
    STATUS: Literal["online", "dnd", "idle"] = "online"
    
    # Custom status message
    CUSTOM_STATUS: str = "Asleep"
    
    # Display name
    DISPLAY_NAME: str = "Celestial" 