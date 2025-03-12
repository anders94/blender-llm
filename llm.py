bl_info = {
    "name": "Blender LLM",
    "author": "Anders Brownworth",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > LLM",
    "description": "Integrate LLM chat functionality into Blender using Ollama",
    "warning": "",
    "wiki_url": "",
    "category": "3D View",
}

import bpy
from bpy.props import StringProperty, EnumProperty, IntProperty, BoolProperty
import requests
import json
import threading
import re

# Global variables to store configuration
ollama_models = []
chat_history = []
current_response = ""
is_loading = False
timer = None

# Function to get scene information
def get_scene_info():
    scene_info = []
    for obj in bpy.context.scene.objects:
        scene_info.append(f"{obj.name} ({obj.type}): Location {obj.location}, Rotation {obj.rotation_euler}")
    return "\n".join(scene_info)

# Addon preferences
class BlenderLLMPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    ollama_url: StringProperty(
        name="Ollama URL",
        description="URL for the Ollama API",
        default="http://localhost:11434",
    )
    
    auto_execute_code: BoolProperty(
        name="Auto-Execute Code",
        description="Automatically execute Python code from LLM responses",
        default=False,
    )
    
    def draw(self, context):
        layout = self.layout
        layout.prop(self, "ollama_url")
        layout.prop(self, "auto_execute_code")
        layout.operator("llm.refresh_models")

# Timer callback to update UI during streaming
def timer_callback():
    global is_loading
    if is_loading:
        # Force Blender UI update
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)
    return 0.1  # run every 100ms

# Get the addon preferences
def get_prefs():
    return bpy.context.preferences.addons[__name__].preferences

# Operator for handling Enter key press in prompt field
class LLMPromptEnterHandler(bpy.types.Operator):
    bl_idname = "llm.prompt_enter"
    bl_label = "Handle Enter Key in Prompt"
    bl_description = "Submit prompt when Enter is pressed"
    
    def execute(self, context):
        bpy.ops.llm.send_prompt()
        return {'FINISHED'}
        
# Operator to send prompt to Ollama
class LLMSendPrompt(bpy.types.Operator):
    bl_idname = "llm.send_prompt"
    bl_label = "Send"
    bl_description = "Send prompt to LLM"
    
    def execute(self, context):
        global chat_history, is_loading, current_response, timer
        
        scene = context.scene
        prompt = scene.llm_prompt
        model = scene.llm_model
        
        if not prompt.strip():
            return {'CANCELLED'}
        
        # Add user message to chat history
        chat_history.append({"role": "user", "content": prompt})
        
        # Clear prompt field
        scene.llm_prompt = ""
        
        # Set loading flag
        is_loading = True
        current_response = ""
        
        # Add placeholder for assistant response
        chat_history.append({"role": "assistant", "content": ""})
        
        # Register timer for UI updates if not already registered
        if not timer:
            timer = bpy.app.timers.register(timer_callback)
        
        # Start thread to get response
        threading.Thread(target=self.get_llm_response, args=(prompt, model)).start()
        
        return {'FINISHED'}
    
    # Extract Python code from a response
    def extract_python_code(self, response):
        # Look for code blocks with ```python or just ```
        code_blocks = re.findall(r'```(?:python)?(.*?)```', response, re.DOTALL)
        if code_blocks:
            return code_blocks[0].strip()
        return None
    
    def get_llm_response(self, prompt, model):
        global chat_history, is_loading, current_response
        
        try:
            ollama_url = get_prefs().ollama_url
            url = f"{ollama_url}/api/chat"
            
            # Create system message with scene information for context
            system_message = {
                "role": "system",
                "content": (
                    "You are a programming assistant for Blender 3D's Python API. "
                    "I will ask you to perform actions in Blender and you will respond with the corresponding Blender Python commands "
                    "surrounded by three tick marks like this '```'. Do not explain your answer. "
                    "If a command is not possible, respond with `not possible` and a one sentence description of why. "
                    "If my question is not related to Blender, respond with `not possible` and a one sentence description of why. "
                    f"Here is a list of objects in the scene and their locations: {get_scene_info()}"
                )
            }
            
            # Include system message but exclude the placeholder response
            messages = [system_message] + chat_history[:-1]
            
            payload = {
                "model": model,
                "messages": messages,
                "stream": True
            }
            
            # Track if we're in a reasoning block
            in_reasoning_block = False
            reasoning_buffer = ""
            
            with requests.post(url, json=payload, stream=True) as response:
                for line in response.iter_lines():
                    if line:
                        try:
                            data = json.loads(line.decode('utf-8'))
                            if 'message' in data and 'content' in data['message']:
                                chunk = data['message']['content']
                                
                                # Check for reasoning block markers
                                if "<think>" in chunk and not in_reasoning_block:
                                    # We've entered a reasoning block
                                    in_reasoning_block = True
                                    # Add text before the <think> marker
                                    pre_think_text = chunk.split("<think>")[0]
                                    if pre_think_text:
                                        current_response += pre_think_text
                                    reasoning_buffer = chunk[len(pre_think_text):]
                                    
                                elif "</think>" in chunk and in_reasoning_block:
                                    # We're exiting a reasoning block
                                    in_reasoning_block = False
                                    # Add text after the </think> marker
                                    post_think_text = chunk.split("</think>")[1]
                                    if post_think_text:
                                        current_response += post_think_text
                                
                                elif in_reasoning_block:
                                    # We're inside a reasoning block, so buffer the content
                                    reasoning_buffer += chunk
                                    
                                else:
                                    # Regular chunk, add to response
                                    current_response += chunk
                                
                                # Update the last message in chat history (the assistant's response)
                                chat_history[-1]["content"] = current_response
                                
                        except json.JSONDecodeError:
                            continue
            
            # After streaming completes, check for Python code to execute
            if get_prefs().auto_execute_code:
                # Look for Python code in the response
                python_code = self.extract_python_code(current_response)
                if python_code:
                    # Execute the Python code in the main thread using a timer
                    bpy.app.timers.register(lambda: self.execute_python_code(python_code), first_interval=0.1)
        
        except Exception as e:
            # Update the last message in chat history with the error
            chat_history[-1]["content"] = f"Error: {str(e)}"
        
        finally:
            is_loading = False
            
    def execute_python_code(self, code):
        try:
            # Execute the Python code in the Blender environment
            exec(code, {"bpy": bpy})
            return None  # Don't repeat the timer
        except Exception as e:
            # If there's an error, add it to the chat history
            chat_history.append({"role": "assistant", "content": f"Error executing Python code: {str(e)}"})
            return None  # Don't repeat the timer
    
# Operator to clear chat
class LLMClearChat(bpy.types.Operator):
    bl_idname = "llm.clear_chat"
    bl_label = "Clear History"
    bl_description = "Clear chat history"
    
    def execute(self, context):
        global chat_history
        chat_history = []
        return {'FINISHED'}

# Operator to refresh models
class LLMRefreshModels(bpy.types.Operator):
    bl_idname = "llm.refresh_models"
    bl_label = "Refresh Models"
    bl_description = "Refresh available models from Ollama"
    
    def execute(self, context):
        global ollama_models
        ollama_url = get_prefs().ollama_url
        
        try:
            url = f"{ollama_url}/api/tags"
            response = requests.get(url)
            data = response.json()
            
            # Extract model names
            if 'models' in data:
                ollama_models = [(model['name'], model['name'], "") for model in data['models']]
                if not ollama_models:
                    ollama_models = [("none", "No models available", "")]
            else:
                ollama_models = [("none", "Failed to load models", "")]
                
            # Update enum property
            bpy.types.Scene.llm_model = EnumProperty(
                name="Model",
                items=ollama_models,
                description="LLM model to use",
            )
            
            return {'FINISHED'}
        
        except Exception as e:
            self.report({'ERROR'}, f"Failed to refresh models: {str(e)}")
            ollama_models = [("none", "Failed to load models", "")]
            return {'CANCELLED'}

# Operator to execute detected Python code
class LLMExecuteCode(bpy.types.Operator):
    bl_idname = "llm.execute_code"
    bl_label = "Execute Code"
    bl_description = "Execute Python code from last response"
    
    def execute(self, context):
        global chat_history
        
        if not chat_history or chat_history[-1]["role"] != "assistant":
            self.report({'ERROR'}, "No assistant response to execute")
            return {'CANCELLED'}
        
        # Extract code from the last response
        code_extractor = LLMSendPrompt.extract_python_code
        python_code = code_extractor(self, chat_history[-1]["content"])
        
        if not python_code:
            self.report({'ERROR'}, "No Python code found in response")
            return {'CANCELLED'}
        
        # Execute the code
        try:
            exec(python_code, {"bpy": bpy})
            self.report({'INFO'}, "Code executed successfully")
            return {'FINISHED'}
        except Exception as e:
            self.report({'ERROR'}, f"Error executing code: {str(e)}")
            chat_history.append({"role": "assistant", "content": f"Error executing Python code: {str(e)}"})
            return {'CANCELLED'}

# Main panel
class LLMPanel(bpy.types.Panel):
    bl_label = "Blender LLM"
    bl_idname = "VIEW3D_PT_llm_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'LLM'
    
    def draw(self, context):
        global chat_history, is_loading
        
        layout = self.layout
        scene = context.scene
        
        # Model selection
        row = layout.row()
        row.label(text="Model:")
        row.prop(scene, "llm_model", text="")
        
        # Auto-execute status
        auto_execute = get_prefs().auto_execute_code
        row = layout.row()
        row.label(text=f"Auto-Execute: {'Enabled' if auto_execute else 'Disabled'}")
        
        # Chat history container
        box = layout.box()
        col = box.column()
        
        # Set chat display height
        chat_height = scene.llm_chat_height
        col.prop(scene, "llm_chat_height", text="Chat Height")
        
        # Create chat area that fits available space without scrolling
        chat_box = col.box()
        scroll = chat_box.column()
        
        # Determine how many messages we can display
        # Calculate approximate available space and determine which messages to show
        # We prioritize showing the most recent messages
        visible_messages = chat_history
        if len(chat_history) > chat_height * 2:  # Rough estimate - each message takes about 2 units
            # Show the most recent messages that can fit
            visible_messages = chat_history[-int(chat_height * 1.5):]
            # Add indicator that messages are hidden
            indicator = scroll.row()
            indicator.alignment = 'CENTER'
            indicator.label(text=f"↑ {len(chat_history) - len(visible_messages)} earlier messages not shown ↑")
        
        # Display messages with minimal formatting - optimized for space
        for message in visible_messages:
            if message["role"] == "user":
                # User message container - minimal padding
                user_box = scroll.box()
                user_box.scale_y = 0.8
                
                # Compact header
                header = user_box.row()
                header.scale_y = 0.6
                header.label(text="You:", icon='USER')
                
                # Message content - efficient use of space
                content = user_box.column(align=True)
                content.scale_y = 0.6
                
                # Just display each line directly without paragraph separation
                lines = [l for l in message["content"].split('\n') if l.strip()]
                for line in lines:
                    row = content.row()
                    row.alignment = 'LEFT'
                    row.label(text=line)
            else:
                # AI message container - minimal padding
                ai_box = scroll.box()
                ai_box.scale_y = 0.8
                
                # Compact header
                header = ai_box.row()
                header.scale_y = 0.6
                header.label(text="AI:", icon='OUTLINER_OB_FONT')
                
                # Message content - efficient use of space
                content = ai_box.column(align=True)
                content.scale_y = 0.6
                
                # Just display each line directly without paragraph separation
                lines = [l for l in message["content"].split('\n') if l.strip()]
                for line in lines:
                    row = content.row()
                    row.alignment = 'LEFT'
                    row.label(text=line)
                
                # Check if this is the last assistant message and has code
                if message == chat_history[-1] and message["role"] == "assistant":
                    code = LLMSendPrompt.extract_python_code(self, message["content"])
                    if code and not auto_execute:
                        # Add compact execute button
                        exec_row = ai_box.row()
                        exec_row.scale_y = 0.7
                        exec_row.operator("llm.execute_code", text="Execute Code", icon='PLAY')
        
        # Loading indicator
        if is_loading:
            layout.label(text="Generating response...")
        
        # Input field and send button
        row = layout.row()
        row.prop(scene, "llm_prompt", text="")
        row.operator("llm.send_prompt", text="", icon='EXPORT')
        
        # Clear chat button
        layout.operator("llm.clear_chat")

# Properties
def register_properties():
    # Create key map for Enter key
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    
    if kc:
        km = kc.keymaps.new(name="3D View", space_type='VIEW_3D')
        kmi = km.keymap_items.new(
            "llm.prompt_enter", 
            type='RET', 
            value='PRESS', 
            ctrl=False, 
            shift=False, 
            alt=False
        )
    
    bpy.types.Scene.llm_prompt = StringProperty(
        name="Prompt",
        description="Enter your prompt here (press Enter to send)",
        default="",
    )
    
    bpy.types.Scene.llm_chat_height = IntProperty(
        name="Chat Height",
        description="Height of the chat display area",
        default=10,
        min=5,
        max=30,
    )
    
    global ollama_models
    if not ollama_models:
        ollama_models = [("llama3", "llama3", "Llama 3")]
    
    bpy.types.Scene.llm_model = EnumProperty(
        name="Model",
        items=ollama_models,
        description="LLM model to use",
    )

# Registration
classes = (
    BlenderLLMPreferences,
    LLMPromptEnterHandler,
    LLMSendPrompt,
    LLMClearChat,
    LLMRefreshModels,
    LLMExecuteCode,
    LLMPanel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    register_properties()
    
    # Try to refresh models on startup
    bpy.app.timers.register(lambda: bpy.ops.llm.refresh_models(), first_interval=1.0)

def unregister():
    # Remove timers if they exist
    if timer and bpy.app.timers.is_registered(timer):
        bpy.app.timers.unregister(timer)
    
    if bpy.app.timers.is_registered(lambda: bpy.ops.llm.refresh_models()):
        bpy.app.timers.unregister(lambda: bpy.ops.llm.refresh_models())
    
    # Remove keyboard shortcut
    wm = bpy.context.window_manager
    kc = wm.keyconfigs.addon
    if kc:
        for km in kc.keymaps:
            if km.name == '3D View':
                for kmi in km.keymap_items:
                    if kmi.idname == 'llm.prompt_enter':
                        km.keymap_items.remove(kmi)
                        break
    
    # Unregister classes
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    # Delete properties
    del bpy.types.Scene.llm_prompt
    del bpy.types.Scene.llm_model
    del bpy.types.Scene.llm_chat_height

if __name__ == "__main__":
    register()
